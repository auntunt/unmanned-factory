"""跑批循环。把「人敲一次 factory run」变成「工厂自己把队列吃干」。

这一层刻意**不**碰派发本身：它拿一个 dispatch 回调，只负责认领、计数、
停机和落地。分级、监工、沙箱、规则库全在 Dispatcher 里，循环无权放宽
任何一条 —— D 类硬闸门照样在派发前拦，循环看到的只是一个
state='blocked' 的结果。

三个刻意的默认值：

1. **预算默认有上限，不是无限。** 无人循环最危险的失败模式不是跑错，
   是没人看着的时候一直跑。默认 5 美元；要无限得显式 `--budget-usd 0`，
   而且会打印一行警告。忘记加 flag 的后果应该是「早停」，不是「刷卡」。

2. **默认 watch，不是 drain。** 「无人」的意思是队列空了它还在等下一个，
   而不是空了就退出。要一次性跑完就退用 `--idle drain`（适合 cron）。

3. **收到 SIGINT/SIGTERM 先把手上这个跑完再退。** 中途硬杀会留下
   running/ 里的孤儿条目和一棵没人验收的 worktree。等一轮的代价是几分钟，
   不等的代价是人来收拾现场。
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal

from factory.backlog.store import Backlog, Claim

# outcome 用 Dispatcher 的原词（见 store.OUTCOME_DIR），循环只多一个 error。
Outcome = Literal["merged", "escalated", "blocked_hard_gate", "error"]

DEFAULT_BUDGET_USD = 5.0


class Idle(StrEnum):
    """队列空了怎么办。"""

    ONCE = "once"      # 空了就退（也用于「只跑一个」）
    DRAIN = "drain"    # 抽干再退，适合 cron
    WATCH = "watch"    # 继续等下一个，这才是「无人」


@dataclass(frozen=True)
class TaskRun:
    """一次派发的结果，循环只认这三个字段。

    Dispatcher 的 DispatchReport 由调用方翻译过来 —— 循环不 import
    dispatcher，这样 shell 探针和单测可以塞一个假 dispatch 进来。
    """

    outcome: Outcome
    cost_usd: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class LoopLimits:
    """三个上限都是 0 = 不限，但**预算默认不是 0**。见模块 docstring。"""

    max_tasks: int = 0
    budget_usd: float = DEFAULT_BUDGET_USD
    max_runtime_s: float = 0.0
    poll_s: float = 5.0
    idle: Idle = Idle.WATCH


@dataclass
class LoopReport:
    dispatched: int = 0
    merged: int = 0
    escalated: int = 0
    blocked: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    recovered: int = 0
    idle_polls: int = 0
    stopped_by: str = ""
    trail: list[tuple[str, str]] = field(default_factory=list)

    def bump(self, outcome: str) -> None:
        """按 outcome 计数。未知 outcome 记成 errors 而不是静默丢掉 ——
        计数对不上总数的报表比没有报表更难查。"""
        field_of = {"merged": "merged", "escalated": "escalated",
                    "blocked_hard_gate": "blocked"}
        name = field_of.get(outcome, "errors")
        setattr(self, name, getattr(self, name) + 1)

    def summary(self) -> str:
        return (f"派发 {self.dispatched}：合并 {self.merged} / "
                f"升级 {self.escalated} / 硬闸门 {self.blocked} / "
                f"异常 {self.errors}，花费 ${self.cost_usd:.4f}")


class BacklogLoop:
    """认领 → 派发 → 归档，直到某个停机条件成立。"""

    def __init__(
        self,
        backlog: Backlog,
        dispatch: Callable[[Path], TaskRun],
        *,
        limits: LoopLimits | None = None,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backlog = backlog
        self._dispatch = dispatch
        self._limits = limits or LoopLimits()
        self._log = log
        self._sleep = sleep
        self._now = now
        self._stopping = ""

    # ---------- 停机 ----------

    def request_stop(self, reason: str) -> None:
        """请求在**当前任务跑完之后**退出。不打断正在跑的派发。"""
        if not self._stopping:
            self._stopping = reason
            self._log(f"\n收到 {reason} —— 手上这个任务跑完就退（再按一次强杀，"
                      f"但 running/ 会留下孤儿条目）")

    def install_signal_handlers(self) -> None:
        """把 SIGINT/SIGTERM 接成优雅停机。只在主线程有效。

        第一次收到信号只置标志；用户再按一次会走 Python 默认行为（KeyboardInterrupt），
        因为我们把 handler 恢复回默认了 —— 一个「怎么都停不下来」的无人循环
        比一个留下孤儿的循环更糟。
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            def handler(signum, _frame, _sig=sig):  # pragma: no cover - 信号路径
                signal.signal(_sig, signal.SIG_DFL)
                self.request_stop(signal.Signals(signum).name)
            signal.signal(sig, handler)

    def _limit_hit(self, report: LoopReport, started: float) -> str:
        lim = self._limits
        if self._stopping:
            return self._stopping
        if lim.idle is Idle.ONCE and report.dispatched >= 1:
            return "只跑一个（--idle once）"
        if lim.max_tasks and report.dispatched >= lim.max_tasks:
            return f"达到 --max-tasks {lim.max_tasks}"
        if lim.budget_usd and report.cost_usd >= lim.budget_usd:
            return (f"达到预算上限 ${lim.budget_usd:.2f}"
                    f"（已花 ${report.cost_usd:.4f}）")
        if lim.max_runtime_s and (self._now() - started) >= lim.max_runtime_s:
            return f"达到 --max-runtime {lim.max_runtime_s:g}s"
        return ""

    # ---------- 主循环 ----------

    def run(self) -> LoopReport:
        started = self._now()
        report = LoopReport()

        stale = self._backlog.recover()
        for entry in stale:
            report.recovered += 1
            self._log(f"[recover] {entry.task_id} 上次没跑完 → needs-human")

        while True:
            stop = self._limit_hit(report, started)
            if stop:
                report.stopped_by = stop
                break

            claim = self._backlog.claim_next()
            if claim is None:
                if self._limits.idle is not Idle.WATCH:
                    report.stopped_by = (
                        "队列空（--idle once）" if self._limits.idle is Idle.ONCE
                        else "队列已抽干")
                    break
                report.idle_polls += 1
                self._sleep(self._limits.poll_s)
                continue

            report.dispatched += 1
            self._log(f"\n[{report.dispatched}] {claim.task_id}  "
                      f"→ 派发（已花 ${report.cost_usd:.4f}）")
            run = self._one(claim)
            report.cost_usd += run.cost_usd
            report.bump(run.outcome)
            self._backlog.finish(claim, run.outcome, note=run.note)
            self._log(f"    {run.outcome} "
                      f"(+${run.cost_usd:.4f})  {run.note}".rstrip())

        self._log(f"\n== 停机：{report.stopped_by}")
        self._log(report.summary())
        return report

    def _one(self, claim: Claim) -> TaskRun:
        """跑一个任务。派发抛异常不许把循环带下去。

        一个任务的 YAML 写坏了、worktree 开不出来、CLI 里某处 raise ——
        无人循环碰到这些必须继续处理下一个，否则一条坏任务能让整个队列停摆
        （而且是在没人看着的时候停）。异常归档成 error，原文进 .result.json。
        """
        try:
            return self._dispatch(claim.path)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            return TaskRun(
                outcome="error",
                note=f"{type(exc).__name__}: {exc}",
            )
