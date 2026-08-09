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

4. **连续 2 个任务的花费记不上账就停机。** 预算闸门读的是 CLI 打出的
   total_cost_usd，而被我们 kill 掉的超时进程永远不打那行 —— 它的花费在账上
   是 $0。于是「反复超时」这一种失败模式能绕开 --budget-usd 无限烧下去
   （实测：一次 900s 超时记 $0.0000，transcript 里 ~232k input tokens）。
   熔断器不猜价格，只数「有多少次派发的价格是假的」，连续到 2 次就停。
   为什么不是 1 次：单个任务超时是设计里的正常出口，一次就停机等于让一条
   坏任务停掉整夜。连续两次说明坏的是环境，那才该叫人。
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal

from factory.backlog.journal import Journal
from factory.backlog.store import LOG, Backlog, Claim

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
    """一次派发的结果，循环只认这几个字段。

    Dispatcher 的 DispatchReport 由调用方翻译过来 —— 循环不 import
    dispatcher，这样 shell 探针和单测可以塞一个假 dispatch 进来。

    unpriced 是「cost_usd 这个数低估了」的标记，不是「花了 0 块」。超时被
    kill 的 attempt 拿不到 CLI 的 total_cost_usd payload，于是记 $0 —— 而它
    真的烧了 token。预算闸门只看 cost_usd 的话，一个反复超时的任务在账上
    永远免费，闸门永远不响。这个布尔是熔断器唯一的输入。
    """

    outcome: Outcome
    cost_usd: float = 0.0
    note: str = ""
    unpriced: bool = False


@dataclass(frozen=True)
class LoopLimits:
    """上限都是 0 = 不限，但**预算和熔断默认不是 0**。见模块 docstring。"""

    max_tasks: int = 0
    budget_usd: float = DEFAULT_BUDGET_USD
    max_runtime_s: float = 0.0
    poll_s: float = 5.0
    idle: Idle = Idle.WATCH
    # 连续几个任务的花费都是「不计价的」就停机。默认 2 而不是 1：单个任务
    # 超时是正常的（3 轮 × 900s 上限本来就是设计里的），一次超时就停机会让
    # 一条坏任务把整夜的队列停掉。连续两个都在烧不计价的钱，说明烧的不是
    # 这个任务而是环境（CLI 挂了、网断了、模型不回话），那才该停。
    max_unpriced_streak: int = 2


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
    # 当前连续不计价的次数（一有计价的派发就归零），和整轮的累计次数。
    # 两个都留：熔断看前者，早上看报表想知道的是后者。
    unpriced_streak: int = 0
    unpriced_total: int = 0
    #: 因前置永远等不到而被搬去 needs-human 的条目数。单独一栏而不是并进
    #: errors：errors 是「跑了但出错」，这些一次都没跑，混一起会让
    #: 「派发 0 / 异常 3」读起来像三次失败的派发。
    dep_deadlocked: int = 0

    def bump(self, outcome: str) -> None:
        """按 outcome 计数。未知 outcome 记成 errors 而不是静默丢掉 ——
        计数对不上总数的报表比没有报表更难查。"""
        field_of = {"merged": "merged", "escalated": "escalated",
                    "blocked_hard_gate": "blocked"}
        name = field_of.get(outcome, "errors")
        setattr(self, name, getattr(self, name) + 1)

    def summary(self) -> str:
        # 有漏账就在总额后面标出来。不标的话「花费 $0.8」读起来像真的花了
        # $0.8，而实际账单可能是它的几倍 —— 一个已知偏低的数必须自带这个提示。
        leak = (f"（另有 {self.unpriced_total} 次派发未计价，真实花费更高）"
                if self.unpriced_total else "")
        dead = (f"，另有 {self.dep_deadlocked} 条等不到前置（已转 needs-human）"
                if self.dep_deadlocked else "")
        return (f"派发 {self.dispatched}：合并 {self.merged} / "
                f"升级 {self.escalated} / 硬闸门 {self.blocked} / "
                f"异常 {self.errors}，花费 ${self.cost_usd:.4f}{leak}{dead}")


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
        journal: Journal | None = None,
    ) -> None:
        self._backlog = backlog
        self._dispatch = dispatch
        self._limits = limits or LoopLimits()
        self._log = log
        self._sleep = sleep
        self._now = now
        self._stopping = ""
        # 默认落在队列的 log/ 里。这个目录在 store 里一直被创建但没人写过 ——
        # 一个建好就空着的目录比没有这个目录更容易让人以为「日志在别处」。
        self._journal = journal or Journal(backlog.dir(LOG))

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
        # 熔断放在最后一条：前面几条是「计划内的收工」，这条是「有东西坏了」。
        # 顺序有意义 —— 同时命中时报表上该显示的是计划内那条原因。
        if (lim.max_unpriced_streak
                and report.unpriced_streak >= lim.max_unpriced_streak):
            return (f"连续 {report.unpriced_streak} 个任务的花费未计价"
                    f"（--max-unpriced-streak {lim.max_unpriced_streak}）"
                    f"—— 预算闸门已失效，停机等人看")
        return ""

    # ---------- 主循环 ----------

    def run(self) -> LoopReport:
        """跑到某个停机条件成立。

        run_end 走 finally，所以异常退出也会落一行 —— 而**没有** run_end 的
        run_start 正是 Rollup 用来数「循环自己崩了」的信号。
        """
        report = LoopReport()
        self._journal.event("run_start", queue=str(self._backlog.root),
                            budget_usd=self._limits.budget_usd,
                            idle=str(self._limits.idle))
        try:
            return self._run(report)
        finally:
            self._journal.event("run_end", stopped_by=report.stopped_by,
                                dispatched=report.dispatched,
                                cost_usd=round(report.cost_usd, 6),
                                unpriced_total=report.unpriced_total,
                                merged=report.merged,
                                escalated=report.escalated,
                                blocked=report.blocked,
                                errors=report.errors,
                                dep_deadlocked=report.dep_deadlocked)

    def _park_deadlocks(self, report: LoopReport):
        """把等不到前置的条目搬去 needs-human 并记日志。

        只在「认领不到东西」时调用，不在每轮开头：running/ 里正在跑的那个
        可能马上就合并，早一秒判死锁就会把它的后继误杀。
        """
        dead = self._backlog.park_deadlocked()
        for entry in dead:
            report.dep_deadlocked += 1
            self._log(f"[deadlock] {entry.task_id} 前置永远等不到 → "
                      f"needs-human：{entry.reason}")
            self._journal.event("dep_deadlock", task_id=entry.task_id,
                                missing=list(entry.missing),
                                reason=entry.reason)
        return dead

    def _run(self, report: LoopReport) -> LoopReport:
        started = self._now()

        stale = self._backlog.recover()
        for entry in stale:
            report.recovered += 1
            self._log(f"[recover] {entry.task_id} 上次没跑完 → needs-human")
            self._journal.event("recover", task_id=entry.task_id,
                                reason=entry.reason)

        while True:
            stop = self._limit_hit(report, started)
            if stop:
                report.stopped_by = stop
                break

            claim = self._backlog.claim_next()
            if claim is None:
                # 认领不到有三种原因，**必须分开**：队列真空了、都被别的
                # worker 抢走了、有活但前置没合并。第三种如果被算进「队列
                # 已抽干」，一整夜什么都没跑会显示成正常收工。
                waiting = self._backlog.blocked_by_deps()
                if waiting:
                    for entry in self._park_deadlocks(report):
                        waiting = tuple(w for w in waiting
                                        if w[0].stem != entry.task_id)
                if waiting:
                    if self._limits.idle is Idle.WATCH:
                        report.idle_polls += 1
                        self._sleep(self._limits.poll_s)
                        continue
                    report.stopped_by = (
                        f"{len(waiting)} 条在等前置合并"
                        f"（--idle {'once' if self._limits.idle is Idle.ONCE else 'drain'}）")
                    break
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
            t0 = self._now()
            run = self._one(claim)
            report.cost_usd += run.cost_usd
            # 归零而不是自减：熔断要的是「连续」。中间夹一个正常计价的任务
            # 就说明环境还活着，前面那次超时是任务自己的问题。
            if run.unpriced:
                report.unpriced_streak += 1
                report.unpriced_total += 1
            else:
                report.unpriced_streak = 0
            report.bump(run.outcome)
            self._backlog.finish(claim, run.outcome, note=run.note)
            self._log(f"    {run.outcome} "
                      f"(+${run.cost_usd:.4f})  {run.note}".rstrip())
            self._journal.event("dispatch", task_id=claim.task_id,
                                outcome=run.outcome,
                                cost_usd=round(run.cost_usd, 6),
                                unpriced=run.unpriced,
                                wall_clock_s=round(self._now() - t0, 3),
                                note=run.note)

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
            # **unpriced=True**：异常可能发生在派发之后（落地那一步炸了、
            # 审计库写不进去），此时 attempt 已经跑完、钱已经烧了，而我们
            # 拿不到 attempt_ids，一分钱都入不了账。默认 cost=0/unpriced=False
            # 的话，这个任务在预算账上免费且熔断器看不见 —— 反复发生就能
            # 绕开 --budget-usd 无限烧。实测：landing 抛异常 → (0.0, False)。
            #
            # 不猜价格：这里没有 attempt_ids，也没有价目表。标记「这个数
            # 低估了」就够 —— 那正是 unpriced 的语义，见 TaskRun 的 docstring。
            #
            # 代价是 YAML 写坏这种「派发前就炸」的情况也会被标 unpriced，
            # 它真的没花钱。宁可这样：熔断器多停一次机（人来看一眼就知道是
            # YAML 坏了）比漏掉一条烧钱的路便宜。而且派发前炸的任务是坏任务，
            # 连续两条坏任务本身也值得停下来。
            return TaskRun(
                outcome="error",
                note=f"{type(exc).__name__}: {exc}",
                unpriced=True,
            )
