"""第二个 harness：把任意 CLI 命令当 worker 用。

存在的理由不是「多一个选择」，而是**验证 HarnessAdapter 这个 Protocol 真的可替换**。
P0 只接了 claude_code 一家，"接口已备好"在只有一个实现时是无法证伪的说法。

和 ClaudeCodeAdapter 有一条**相反**的契约，这是本文件最要紧的一点：

    ClaudeCodeAdapter: 退出码永远是 0，只能读 JSON 里的 is_error
    ShellAdapter:      退出码就是真相，没有 JSON 可读

所以两者不能共用结果解析。谁想再加第三个 harness，先确认它属于哪一类。

适用范围：
  - 确定性脚本（codemod、formatter、生成迁移）—— 根本不调模型，零成本跑 A 类任务
  - 退出码有意义的 CLI agent（多数工具如此，claude 是例外）

不适用：输出结构化 JSON 且退出码不可信的 agent —— 那种要单独写 adapter。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from factory.harness import sandbox as sb
from factory.harness.base import AttemptResult, ExitStatus, Limits
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.harness.workspace import capture_diff, diff_hash
from factory.task import Task

PROMPT_ENV = "FACTORY_PROMPT"
MODEL_ENV = "FACTORY_MODEL"
TASK_ENV = "FACTORY_TASK_ID"

_PROMPT_SLOT = "{prompt}"
_WORKSPACE_SLOT = "{workspace}"


class ShellAdapter:
    """跑一条 argv，用退出码判成败，用 git diff 拿改动。

    prompt 有两条送达路径，都不经过 shell：
      1. argv 里的 {prompt} 占位符 —— 直接替换成一个 argv 元素
      2. 环境变量 FACTORY_PROMPT —— 总是设置，脚本可以自己读

    **刻意不用 shell=True。** prompt 是任务文件里的自由文本，将来还可能来自
    PRD 生成器。走 shell 就等于把它当命令拼起来执行，一个反引号就能越权。
    argv 列表没有这个洞，代价只是不能用管道 —— 想用管道就把它写进脚本里。
    """

    def __init__(
        self,
        argv_template: list[str],
        *,
        name: str = "shell",
        env: dict[str, str] | None = None,
        exit_code_ok: tuple[int, ...] = (0,),
        probe_version: bool = False,
        sandbox: bool = False,
    ) -> None:
        if not argv_template:
            raise ValueError("argv_template 不能为空")
        self._argv_template = list(argv_template)
        # name 是实例属性而不是类属性：审计表要区分 codex / aider / 自研脚本，
        # 全记成 "shell" 就没法按 harness 比命中率了。
        self.name = name
        self._extra_env = dict(env or {})
        self._exit_code_ok = exit_code_ok
        self._probe_version = probe_version
        # 沙箱包在 run() 的 argv 外层，不包 version() 探针 —— 探针已经跑在
        # 空临时目录里，而那个目录不在策略白名单内，包了反而会假失败。
        self._sandbox = sandbox

    def version(self) -> str:
        """审计用的版本串。默认**不执行**目标程序。

        这里踩过一个真实的坑（2026-08-07）：原来的实现直接跑
        `argv[0] --version` 且没设 cwd，结果每次 run() 都在**调用方的当前目录**
        把 worker 脚本完整跑了一遍 —— 测试往编排层自己的仓库里写了 out.py
        并被 `git add -A` 提交进去（10a0d9f）。

        任意可执行文件不保证认 `--version`：裸脚本会忽略这个 flag 直接干活。
        所以默认改成对文件内容取哈希，不执行任何东西。真需要问版本号的
        （codex/aider 这类正规 CLI）显式传 probe_version=True 打开，
        而且探针跑在空临时目录里，不在 workspace 也不在调用方 cwd。
        """
        return sb.tag_version(self._raw_version(), self._sandbox)

    def _raw_version(self) -> str:
        if not self._probe_version:
            return self._static_version()
        with tempfile.TemporaryDirectory(prefix="factory-shell-ver-") as clean:
            try:
                # 探针也走 run_bounded 的理由见 ClaudeCodeAdapter.version()。
                # 这里更需要：probe_version=True 的对象是任意可执行文件。
                proc = run_bounded([self._argv_template[0], "--version"],
                                   cwd=clean, timeout_s=30.0)
            except (OSError, ProcTimeout):
                return self._static_version()
        lines = (proc.stdout or proc.stderr or "").strip().splitlines()
        return lines[0][:64] if lines else self._static_version()

    def _static_version(self) -> str:
        """不执行程序时的版本串：可执行文件内容的短哈希。

        比 "unknown" 有用 —— 脚本改了哈希就变，审计里能看出「这次和上次
        用的不是同一个 worker」，而这正是 harness_version 字段的用途。
        """
        exe = shutil.which(self._argv_template[0]) or self._argv_template[0]
        try:
            digest = hashlib.sha256(Path(exe).read_bytes()).hexdigest()[:12]
        except OSError:
            return "unknown"
        return f"sha256:{digest}"

    def _argv(self, task: Task, workspace: Path) -> list[str]:
        return [
            (
                task.prompt
                if arg == _PROMPT_SLOT
                else str(workspace)
                if arg == _WORKSPACE_SLOT
                else arg
            )
            for arg in self._argv_template
        ]

    def _env(self, task: Task, model: str | None) -> dict[str, str]:
        env = {**os.environ, **self._extra_env}
        env[PROMPT_ENV] = task.prompt
        env[TASK_ENV] = task.task_id
        if model:
            env[MODEL_ENV] = model
        return env

    def run(
        self,
        task: Task,
        workspace: Path,
        limits: Limits,
        *,
        model: str | None = None,
    ) -> AttemptResult:
        version = self.version()
        started = time.monotonic()

        stack = contextlib.ExitStack()
        try:
            argv = self._argv(task, Path(workspace))
            env = self._env(task, model)
            if self._sandbox:
                argv, env_overrides = sb.prepare(argv, Path(workspace), stack)
                env.update(env_overrides)
            # 见 proc 模块：超时杀整棵树。shell harness 更需要这个 ——
            # argv 通常是个 sh 脚本，它派生的东西 sh 自己都不管。
            proc = run_bounded(
                argv,
                cwd=workspace,
                env=env,
                timeout_s=limits.timeout_s,
            )
        except ProcTimeout as exc:
            return self._result(
                workspace,
                ExitStatus.TIMEOUT,
                version,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_text=str(exc),
            )
        except OSError as exc:
            return self._result(
                workspace,
                ExitStatus.ERROR,
                version,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_text=f"cannot launch {self._argv_template[0]}: {exc}",
            )
        finally:
            # 每条返回路径都要清掉策略临时文件，包括上面两个 except 里的 return。
            stack.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        ok = proc.returncode in self._exit_code_ok
        # 报错时把 stderr 带上（截断）：这段会进 supervisor 的 withheld 列表，
        # 也会经 redact() 落审计，所以脚本打了密钥也不会写进库。
        error_text = (
            ""
            if ok
            else f"exit {proc.returncode}: "
            + ((proc.stderr or proc.stdout or "").strip()[:2000])
        )
        return self._result(
            workspace,
            ExitStatus.OK if ok else ExitStatus.ERROR,
            version,
            elapsed_ms=elapsed_ms,
            error_text=error_text,
        )

    def _result(
        self,
        workspace: Path,
        status: ExitStatus,
        version: str,
        *,
        elapsed_ms: int,
        error_text: str = "",
    ) -> AttemptResult:
        """成败都捕获 diff —— 和 ClaudeCodeAdapter 一致：失败也可能留下改动。

        tokens / cost 恒为 0，而且这不是「还没实现」：脚本没有 token 消耗。
        命中率报表里 shell 任务的单位成本因此是 $0，这是真话。
        """
        try:
            diff, paths = capture_diff(Path(workspace))
        except RuntimeError:
            diff, paths = "", ()
        return AttemptResult(
            exit_status=status,
            diff=diff,
            diff_hash=diff_hash(diff),
            changed_paths=paths,
            wall_clock_ms=elapsed_ms,
            harness_version=version,
            error_text=error_text,
        )
