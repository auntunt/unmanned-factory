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
from factory.harness import procfence
from factory.harness import sandbox as sb
from factory.harness.base import AttemptResult, ExitStatus, Limits, ToolCall
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.harness.transcript import find_transcript, parse_tool_calls, projects_root
from factory.harness.workspace import capture_diff, diff_hash
from factory.permission.broker import GateBroker
from factory.permission.gate import PermissionGate
from factory.task import Task


#: 沙箱模式下用哪个 python 跑 hook 脚本。
#:
#: **不能用 sys.executable。** venv 通常就在工厂仓库底下
#: （`<repo>/.venv/bin/python`），而沙箱第 2 段 `--tmpfs <factory_root>` 把整个
#: 仓库盖掉了 —— 于是 hook 的 command 指向一个在沙箱里不存在的解释器，
#: 进程起不来，claude 那边当成 hook 故障处理 = **静默放行**。
#: 门看起来接上了（settings 有、broker 在跑），实际一个动作都拦不住。
#:
#: 这就是 hook.py 只准用标准库的原因：系统 python 里没有本项目的依赖。
_SANDBOX_HOOK_PYTHON = "/usr/bin/python3"


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
        gate: PermissionGate | None = None,
    ) -> None:
        self._binary = binary
        self._projects_root = projects_root
        # 默认关：沙箱只在 macOS 上有，开了在别的平台会直接抛。
        # 显式开启和 worktree 一个道理 —— 最便宜的路径不该因为多了一层而变贵。
        self._sandbox = sandbox
        # 权限门。None = 不接（默认），worker 行为跟接门之前完全一致。
        # 传进来的 gate 由调用方持有，跑完从 gate.stats 取事件落库 ——
        # adapter 不碰数据库，和它不碰 store 的既有约定一致。
        self._gate = gate
        #: 上一次 run() 里 worker 被 hook 拦下的动作。CLI 在结果 JSON 的
        #: permission_denials 里给这个，是「门真的生效了」的**独立证据**：
        #: gate.stats 是我们自己记的账，这个是 CLI 那边记的。两边对不上就说明
        #: 接线断了（比如 hook 静默放行），而那种断法从我们自己的统计里看不出来。
        self.last_denials: tuple[dict, ...] = ()
        #: 最近一次 run() 的 broker。drain_gate_events 要靠它拿加锁的 drain。
        self._broker: GateBroker | None = None

    def drain_gate_events(self) -> tuple[dict, ...]:
        """取出并清空权限门累计的非 ALLOW 事件，形状对齐
        `store.record_permission_event` 的关键字参数。

        dispatcher 每轮 attempt 调一次（见 `_record_gate_events`）。
        adapter 自己不落库：它不持有 store，这个分工跟 run() 只返回
        AttemptResult、由 dispatcher 决定怎么记是一致的。

        没接门时返回空元组，dispatcher 那边的循环直接不进。
        """
        # 委托给 broker：那边 drain 是加锁的。broker 的生命周期是单次 run()
        # （ExitStack 退出即关），但 stats 挂在 gate 上活得更久，所以 run()
        # 结束后仍能取到这一轮的事件。
        if self._broker is None:
            return ()
        return self._broker.drain_events()

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

        # 进程围栏在**派发前**算：算的是当下这一刻的余量。放到循环外或
        # 构造函数里算的话，连跑几百个任务时用的还是进程刚起来时的数字，
        # 而那时机器可能已经被别的 attempt 填满了。
        fence = procfence.plan()

        # 这次执行的 transcript 在哪棵树下。沙箱模式下不是宿主的 ~/.claude
        # （那边被 --tmpfs /home 盖掉了，worker 写进 tmpfs，沙箱一销毁就没了），
        # 而是 prepare() 给的那个出口目录。声明在 with 之外：find_transcript
        # 在沙箱退出之后才跑，那时 argv/overrides 已经出了作用域。
        transcript_root: Path | None = None

        self.last_denials = ()

        with contextlib.ExitStack() as stack:
            argv = self._argv(task, limits, model)
            env: dict[str, str] | None = None

            # 权限门。**必须在 sb.prepare 之前起**：broker 的目录要作为
            # extra_writable 传给沙箱，而 prepare 一旦拼完 argv 就没法追加挂载了。
            gate_writable: tuple[Path, ...] = ()
            gate_env: dict[str, str] = {}
            broker: GateBroker | None = None
            if self._gate is not None:
                broker = GateBroker(
                    self._gate,
                    python=_SANDBOX_HOOK_PYTHON if self._sandbox else None,
                )
                stack.enter_context(broker)
                argv += ["--settings", broker.settings_json()]
                gate_env[broker.socket_env] = broker.socket_path
                gate_writable = (broker.mount_dir,)
                # 存起来给 drain_gate_events 用。必须走 broker 而不是直接读
                # gate.stats：broker 每个连接一个线程，GateStats.record 会被
                # 并发调用，只有 broker._lock 那条路径是串行化的。
                # 直接摸 gate.stats 会和正在写入的 handler 线程抢，丢的正是
                # 审计要看的非 ALLOW 事件。
                self._broker = broker

            if self._sandbox:
                # 沙箱不可用时**抛**，不静默降级：调用方以为隔离生效了，
                # 实际没有 —— 那比不开沙箱更危险。
                argv, overrides = sb.prepare(
                    argv, Path(workspace), stack, extra_writable=gate_writable
                )
                env = {**os.environ, **overrides, **gate_env}
                # prepare() 把出口目录作为 HOME 交回来；claude 只认 $HOME 来
                # 决定 transcript 写哪儿。用 projects_root() 而不是自己拼
                # ".claude"/"projects"：那个布局归 transcript 模块管，两处
                # 各拼一遍的话哪天 CLI 改了目录名，这里会安静地找不到文件
                # —— 而"找不到"的表现就是 transcript_path=None，正是本次要修的东西。
                #
                # 用 `.get` 而非 `in`+索引：后端没给 HOME（macOS Seatbelt 那边
                # 就没有）时留 None，走下面宿主 ~/.claude 的老路，行为不变。
                if (sandbox_home := overrides.get("HOME")):
                    transcript_root = projects_root(Path(sandbox_home))
            elif gate_env:
                # 非沙箱路径。env 原本是 None（= 继承父进程环境），但 socket
                # 路径必须传下去，所以这里显式复制一份 os.environ 再叠加。
                # 只在接了门的时候才构造：不接门时保持 None，行为一字不变。
                env = {**os.environ, **gate_env}
            try:
                # 不是 subprocess.run：超时要杀掉整棵进程树。claude 是个
                # node 进程，会拉起 MCP server 和 Bash 工具的每条命令，
                # 只 kill 直接子进程的话花钱的那一半会活下来。见 proc 模块。
                proc = run_bounded(
                    argv,
                    cwd=workspace,
                    env=env,
                    timeout_s=limits.timeout_s,
                    # 挂死止损：CLI 会随机起来但不发请求也不退出（CPU 0%、
                    # ep_poll、零网络连接）。没有这一层就得干等满 timeout_s，
                    # 而那种 attempt 记 $0，预算闸门拦不住。
                    stall_timeout_s=limits.stall_timeout_s,
                    # 进程围栏：worker 失控 fork 时别把整机进程表打满
                    # （那时连 sshd 都起不来，人进不去机器看现场）。
                    # plan() 算不出余量时返回 None = 不设，软失败不拦派发。
                    nproc_limit=fence.limit,
                )
            except ProcTimeout as exc:
                return self._result(
                    workspace,
                    ExitStatus.TIMEOUT,
                    version,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_text=str(exc),
                    # 停滞和跑满超时都是 TIMEOUT，但成因不同：前者是环境
                    # 故障（该熔断），后者是任务太大（该换模型重试）。
                    stalled=getattr(exc, "stalled", False),
                )
            except OSError as exc:
                # 围栏说明必须带上。preexec_fn 里的 setrlimit 失败、以及
                # 围栏设太紧导致 exec 前就 fork 不出来，症状都是这条 OSError
                # —— 而 "cannot launch claude" 这句话本身完全看不出跟围栏有关。
                # 这正是 stall_timeout_s 那次踩过的坑：错误信息把人引向
                # 「CLI 装没装」，真因在我们自己加的一层上。
                return self._result(
                    workspace,
                    ExitStatus.ERROR,
                    version,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_text=(
                        f"cannot launch {self._binary}: {exc}\n"
                        f"进程围栏：{fence.note}"
                    ),
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
        # 沙箱模式下先看出口目录；transcript_root 为 None（非沙箱、或后端不给
        # HOME）时退回构造参数 / 宿主 ~/.claude，非沙箱路径一个字没变。
        transcript = (
            find_transcript(session_id, root=transcript_root or self._projects_root)
            if session_id
            else None
        )
        tool_calls: tuple[ToolCall, ...] = (
            parse_tool_calls(transcript) if transcript else ()
        )

        # CLI 那边记的「被拦下的动作」。实测字段形状：
        #   permission_denials:[{tool_name, tool_use_id, tool_input:{...}}]
        # 留着当交叉验证：gate.stats.denied 是我们自己的账，这个是 CLI 的账。
        # 我们记了 deny 而这里是空 → hook 没真接上（最可能是静默放行那条路）。
        denials = payload.get("permission_denials")
        self.last_denials = (
            tuple(d for d in denials if isinstance(d, dict))
            if isinstance(denials, list)
            else ()
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
        stalled: bool = False,
    ) -> AttemptResult:
        """无论成败都捕获 diff —— 失败的 attempt 也可能留下改动，必须入审计。"""
        try:
            diff, paths = capture_diff(Path(workspace))
        except RuntimeError:
            diff, paths = "", ()
        return AttemptResult(
            stalled=stalled,
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
