"""Claude Code adapter。P0 只接这一个 harness。

两个反直觉的地方，改动前先看 Global Constraints：
  1. `claude -p` 的退出码永远是 0 —— 只读 JSON 里的 is_error
  2. `--max-turns` 能用但 `--help` 里没有 —— 未文档化依赖，所以做成可选

第三件（spec §10 风险 6）：**这份 JSON 没有文档、没有版本号**，字段随时可能
改名。而这里全是 `payload.get(k, 默认)` —— 改名不会报错，会静默降级：

  total_cost_usd 改名 → 每次派发都记 $0 → 预算上限形同虚设
  is_error       改名 → 失败的任务被当成成功 → 监工去核一个空 diff
  usage          改名 → token 数全 0 → 单位成本指标失效

所以解析完要**显式检查承重字段在不在**（`_missing_fields`）。不在就落进
error_text 并把这次派发标成漏账 —— 和超时被 kill 是同一类事（花了钱但记不上），
所以走同一个熔断器。见 factory/cli.py 的 is_untracked_spend。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from factory.harness import drift
from factory.harness import sandbox as sb
from factory.harness.base import AttemptResult, ExitStatus, Limits, ToolCall
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.harness.transcript import find_transcript, parse_tool_calls
from factory.harness.workspace import capture_diff, diff_hash
from factory.task import Task


#: 漂移检测搬去了 `factory/harness/drift.py` —— 读这份 JSON 的不止这里，
#: 模型监工读的是同一个 CLI 的同一种输出，而它比这一路贵得多。
#: 这两个别名留着是因为外面（含测试）已经在按这个名字引用。
DRIFT_MARKER = drift.DRIFT_MARKER
_LOAD_BEARING = drift.LOAD_BEARING
_missing_fields = drift.missing_fields


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(
        self,
        binary: str = "claude",
        projects_root: Path | None = None,
        *,
        sandbox: bool = False,
    ) -> None:
        self._binary = binary
        self._projects_root = projects_root
        # 默认关：沙箱只在 macOS 上有，开了在别的平台会直接抛。
        # 显式开启和 worktree 一个道理 —— 最便宜的路径不该因为多了一层而变贵。
        self._sandbox = sandbox

    def version(self) -> str:
        """探针跑在空临时目录里，不在调用方 cwd。

        没设 cwd 的话，被探的程序就在**编排层自己的仓库**里执行一遍。
        真实 claude --version 只打印版本号，看起来没事；但换成任何会写文件的
        可执行体（测试里的假 harness、包装脚本），它就会往这个仓库里拉屎 ——
        实际发生过：out.py 被 git add -A 提交进了 10a0d9f。
        这是 ShellAdapter.version() 同一个坑，两处都修。
        """
        # 探针也走 run_bounded：一个挂住的 `--version` 同样会留下整棵树。
        # 实测里那 12 个孤儿有一半是探针留的（每次 attempt 前探一次，
        # 每次都等满 30s 再放着不管）。
        try:
            with tempfile.TemporaryDirectory(prefix="factory-cc-ver-") as clean:
                proc = run_bounded([self._binary, "--version"],
                                   cwd=clean, timeout_s=30.0)
            raw = proc.stdout.strip() or "unknown"
        except (OSError, ProcTimeout):
            raw = "unknown"
        return sb.tag_version(raw, self._sandbox)

    def _argv(self, task: Task, limits: Limits, model: str | None) -> list[str]:
        argv = [
            self._binary,
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
        ]
        if model:
            argv += ["--model", model]
        if limits.max_turns is not None:
            # 未文档化的 flag：--help 里没有，但 CLI 2.1.223 接受。
            # 若哪天报 unknown option，删掉这两行即可降级，不影响正确性。
            argv += ["--max-turns", str(limits.max_turns)]
        return argv

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

        with contextlib.ExitStack() as stack:
            argv = self._argv(task, limits, model)
            env: dict[str, str] | None = None
            if self._sandbox:
                # 沙箱不可用时**抛**，不静默降级：调用方以为隔离生效了，
                # 实际没有 —— 那比不开沙箱更危险。
                argv, overrides = sb.prepare(argv, Path(workspace), stack)
                env = {**os.environ, **overrides}
            try:
                # 不是 subprocess.run：超时要杀掉整棵进程树。claude 是个
                # node 进程，会拉起 MCP server 和 Bash 工具的每条命令，
                # 只 kill 直接子进程的话花钱的那一半会活下来。见 proc 模块。
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
                    error_text=f"cannot launch {self._binary}: {exc}",
                )

        elapsed_ms = int((time.monotonic() - started) * 1000)

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return self._result(
                workspace,
                ExitStatus.ERROR,
                version,
                elapsed_ms=elapsed_ms,
                error_text=(proc.stdout or proc.stderr or "")[:2000],
            )

        # 退出码不可信，只看 is_error
        status = ExitStatus.ERROR if payload.get("is_error") else ExitStatus.OK
        error_text = ""
        if status == ExitStatus.ERROR:
            error_text = " ".join(
                str(payload.get(k, ""))
                for k in ("subtype", "stop_reason", "terminal_reason", "result")
            ).strip()

        # 格式漂移检查（spec §10 风险 6）。放在读字段**之前**：
        # 下面每个 .get 都带默认值，漂移之后它们全都安静地返回 0 / None。
        #
        # 判成 ERROR 而不是只加个标记 —— **一份我们读不懂的输出不能当成功**。
        # 最恶劣的漂移正是 is_error 自己改名：那时 status 会是 OK，
        # 任务一路走到监工、拿着一个可能是空的 diff 判绿、然后 merge。
        # fail-closed 之后它变成这一轮红，走 dispatcher 已有的 harness 报错路
        # （claim 的 got 里带 error_text），漏账熔断器也就能看见它。
        missing = _missing_fields(payload)
        if missing:
            status = ExitStatus.ERROR
            error_text = f"{drift.drift_text(missing)}\n{error_text}".strip()

        # `usage` 只算最后一轮，多轮调用会系统性低估；真数在 modelUsage。
        # 实测同一次调用 usage.in=27415 而 modelUsage 累计 77856。见 drift 模块。
        tok_in, tok_out = drift.token_split(payload)
        session_id = payload.get("session_id")
        transcript = (
            find_transcript(session_id, root=self._projects_root)
            if session_id
            else None
        )
        tool_calls: tuple[ToolCall, ...] = (
            parse_tool_calls(transcript) if transcript else ()
        )

        return self._result(
            workspace,
            status,
            version,
            elapsed_ms=payload.get("duration_ms") or elapsed_ms,
            error_text=error_text,
            tokens_in=tok_in,
            tokens_out=tok_out,
            cost_usd=float(payload.get("total_cost_usd", 0.0) or 0.0),
            session_id=session_id,
            transcript_path=str(transcript) if transcript else None,
            tool_calls=tool_calls,
        )

    def _result(
        self,
        workspace: Path,
        status: ExitStatus,
        version: str,
        *,
        elapsed_ms: int,
        error_text: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        session_id: str | None = None,
        transcript_path: str | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> AttemptResult:
        """无论成败都捕获 diff —— 失败的 attempt 也可能留下改动，必须入审计。"""
        try:
            diff, paths = capture_diff(Path(workspace))
        except RuntimeError:
            diff, paths = "", ()
        return AttemptResult(
            exit_status=status,
            diff=diff,
            diff_hash=diff_hash(diff),
            changed_paths=paths,
            transcript_path=transcript_path,
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            wall_clock_ms=elapsed_ms,
            session_id=session_id,
            harness_version=version,
            error_text=error_text,
        )
