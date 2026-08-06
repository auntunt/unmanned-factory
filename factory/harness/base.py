"""harness 适配契约。spec §7.1。

刻意保持小：一个 run()。换 harness 只需要再实现一次这个 Protocol，
编排层、监工层、审计层都不用动。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from factory.task import Task


class ExitStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ToolCall:
    name: str
    call_id: str
    is_error: bool = False


@dataclass(frozen=True)
class Limits:
    max_turns: int | None = None
    timeout_s: int = 900


@dataclass(frozen=True)
class AttemptResult:
    exit_status: ExitStatus
    diff: str = ""
    diff_hash: str | None = None
    changed_paths: tuple[str, ...] = ()
    transcript_path: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_clock_ms: int = 0
    session_id: str | None = None
    harness_version: str = "unknown"
    error_text: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_status == ExitStatus.OK


class HarnessAdapter(Protocol):
    name: str

    def run(
        self,
        task: Task,
        workspace: Path,
        limits: Limits,
        *,
        model: str | None = None,
    ) -> AttemptResult: ...
