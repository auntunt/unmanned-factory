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
    #: None = 不检测（只保留 timeout_s 那道墙）。**默认必须是 None。**
    #:
    #: 为什么默认关掉：ClaudeCodeHarness 用 `--output-format json`，那是
    #: **非流式**的 —— claude 全程不输出任何东西，跑完才一次性吐出整个 JSON。
    #: 在这种格式下「无输出增量」不代表挂死，它是正常状态。开了就会把每个
    #: 真实任务都杀掉。
    #:
    #: 血的教训（2026-08-19 实测）：默认设 120.0 之后投一个真任务，三轮
    #: haiku/sonnet/opus 的 wall_clock_ms 是 120098 / 120097 / 120099 ——
    #: 毫秒级贴着阈值，diff_hash 三轮相同（都是空 diff）。全被自己的停滞
    #: 检测杀掉，报成 `cannot launch claude: [Errno 9] Bad file descriptor`
    #: （管道已被 _drain 关闭，_reap 再 communicate 时抛的 OSError），
    #: 一路误导到「启动失败」。
    #:
    #: 当初的验证之所以没抓到：拿 `--output-format stream-json` 测的，
    #: 那个格式一直吐帧，194s 都不会触发。**用错格式验证等于没验证。**
    #:
    #: 什么时候可以开：调用方明确知道自己跑的是流式命令（stream-json、
    #: pytest -v 这类持续输出的），显式传值。别在这里给默认。
    stall_timeout_s: float | None = None


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
