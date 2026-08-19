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
    #: 连续多久没有任何 stdout/stderr 增量就判 worker 挂死，提前杀掉。
    #: None = 不检测（只保留 timeout_s 那道墙）。
    #:
    #: 120s 是这么定的：实测挂死的进程从头到尾**一个字节都没有**，而正常
    #: 执行在首个 token 之前也有等待 —— claude 要先做 init、读 CLAUDE.md、
    #: 起 MCP server，stream-json 的第一帧不是立刻来的。取 2 分钟给足冷启动
    #: 余量，同时把干等从 900s 砍到 120s：一轮省 13 分钟，三轮阶梯省 40 分钟。
    #:
    #: 不要用「总时长」代替它 —— 真在生成的慢任务持续吐 token，按总时长杀
    #: 会误杀，按增量杀不会。
    stall_timeout_s: float | None = 120.0


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
    #: True = 这次是「worker 活着但一直不吐字节」被提前杀掉，不是跑满超时。
    #:
    #: 用显式字段而不是让上层搜 error_text 里的关键字：子串匹配会撞车 ——
    #: 搜 'hang' 会被 changed_paths 里的 `c-hang-ed` 命中而恒真，这个坑
    #: 真踩过一次。熔断器要据此判「环境故障，别再往上换模型了」。
    stalled: bool = False

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
