"""Claude Code adapter。P0 只接这一个 harness。

两个反直觉的地方，改动前先看 Global Constraints：
  1. `claude -p` 的退出码永远是 0 —— 只读 JSON 里的 is_error
  2. `--max-turns` 能用但 `--help` 里没有 —— 未文档化依赖，所以做成可选
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from factory.harness import sandbox as sb
from factory.harness.base import AttemptResult, ExitStatus, Limits, ToolCall
from factory.harness.transcript import find_transcript, parse_tool_calls
from factory.harness.workspace import capture_diff, diff_hash
from factory.task import Task


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
        try:
            with tempfile.TemporaryDirectory(prefix="factory-cc-ver-") as clean:
                proc = subprocess.run(
                    [self._binary, "--version"],
                    cwd=clean,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            raw = proc.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
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
                proc = subprocess.run(
                    argv,
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=limits.timeout_s,
                )
            except subprocess.TimeoutExpired:
                return self._result(
                    workspace,
                    ExitStatus.TIMEOUT,
                    version,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_text=f"timeout after {limits.timeout_s}s",
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

        usage = payload.get("usage") or {}
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
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
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
