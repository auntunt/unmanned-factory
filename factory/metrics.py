"""监工裁剪判据。spec §5.1：每个监工算三个数 —— 命中率、漏报数、单位命中成本。

判定规则直接来自 spec §5「resolution 决定告警是否算命中」：
  FAIL + reworked        → 真阳性（worker 改了且改完通过，大概率真）
  FAIL + human_override  → 假阳性（人直接覆盖合并，站 worker 一边）
  FAIL + merged          → 假阳性（同轮其他监工放行、这条被无视）
  FAIL + escalated       → **未定案**，既不算真也不算假
  PASS + linked_defects  → 假阴性（四道监工都放过、事后才炸）

未定案单独计数、且不进命中率分母，是这里唯一不显然的决定：
升级给人的 attempt 人还没判，把它算成任何一边都会让两周后的裁剪建在假数字上。
分母为 0 时 hit_rate 返回 None 而不是 0.0 —— 「没数据」和「命中率 0」是两回事，
前者该继续攒数据，后者该降级为抽检。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from factory.audit.models import (
    NOT_DISPATCHED,
    HumanAction,
    HumanGate,
    Resolution,
    Verdict,
    utc_now,
)
from factory.supervisors.model_base import (
    HARNESS_FAULT_CHECKS,
    SUPERVISOR_ERROR_PREFIX,
)

# FAIL 之后，这些 resolution 说明告警是真的 / 是假的 / 还没判
_TRUE_POSITIVE = {Resolution.REWORKED}
_FALSE_POSITIVE = {Resolution.HUMAN_OVERRIDE, Resolution.MERGED}
_UNADJUDICATED = {Resolution.ESCALATED, Resolution.PENDING}


def _is_fault(verdict) -> bool:
    """监工自身故障 vs 真报了个问题。靠 claim 的 check 前缀区分，
    和 dispatcher._merge_reports 用的是同一个前缀 —— 两处判据必须一致，
    否则「拦了合并」和「算进命中率」会对不上。

    只判监工自己那一种。worker CLI 报错是另一个函数 —— 见 _is_harness_fault。
    """
    return any(
        str(c.get("check", "")).startswith(SUPERVISOR_ERROR_PREFIX)
        for c in (verdict.claims or ())
    )


def _is_harness_fault(verdict) -> bool:
    """worker CLI / 上游坏了，不是这个监工报的警。

    真跑批抓到的：网关回 502，`_blocked("harness", ...)` 挂在 REGRESSION 名下，
    于是回归监工白得一次**真阳性** —— 它什么都没审出来，那次红是我们这一侧的
    网络。而这个数正是用来决定「这个监工值不值它的钱」的，虚高的方向恰好是
    「保留」，也就是不会有人来纠的那一侧。

    判据从 model_base 来（dispatcher 和这里都已经 import 它），不在这边重抄
    一份字面量：抄一份的话，将来加第二个 harness 级 check 名只改一边，
    退化方向是「又变回真阳性」，而那是全绿的。
    """
    return any(
        str(c.get("check", "")) in HARNESS_FAULT_CHECKS
        for c in (verdict.claims or ())
    )


@dataclass(frozen=True)
class SupervisorMetrics:
    role: str
    fired: int = 0              # 报 FAIL 的次数
    passed: int = 0             # 报 PASS 的次数
    true_positives: int = 0
    false_positives: int = 0
    unadjudicated: int = 0      # 报了 FAIL 但人还没定案
    false_negatives: int = 0    # 报 PASS 却事后挂上 defect
    faults: int = 0             # 监工自己坏了（超时、拿不到裁决），不是告警
    #: worker CLI / 上游坏了（502、装的东西不对）。**和 faults 分开数**：
    #: 两者都「不是告警」，但指向的修法完全相反 —— faults 说这个监工该修，
    #: 这一项说监工没毛病、是我们这一侧的网络。混在一起时 verdict_line 会
    #: 建议「先修监工可用性」，而那是一条指错方向的建议，比没有建议更费时间。
    harness_faults: int = 0
    cost_usd: float = 0.0
    tokens: int = 0

    @property
    def adjudicated(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def hit_rate(self) -> float | None:
        """已定案告警中真阳性占比。无已定案样本时返回 None，不编数字。"""
        if self.adjudicated == 0:
            return None
        return self.true_positives / self.adjudicated

    @property
    def cost_per_hit(self) -> float | None:
        """单位命中成本。零命中时返回 None —— 除零和「免费」不是一回事。"""
        if self.true_positives == 0:
            return None
        return self.cost_usd / self.true_positives

    def verdict_line(self) -> str:
        """spec §5.1 的三条裁剪建议，按本监工的数据给一条。"""
        # 故障率高先修故障：命中率是在「它真的审了」的前提下才有意义
        if self.faults and self.faults >= max(1, self.fired):
            return "多数轮次是监工自己故障 → 先修可用性，命中率还谈不上"
        # 上游坏了要说清是**上游**。说成「先修监工可用性」会把人送去翻监工日志，
        # 而那里什么都没有 —— 一条指错方向的建议比没有建议更费时间。
        if self.harness_faults and self.harness_faults >= max(1, self.fired):
            return "多数轮次是 worker CLI / 上游报错 → 先看网关，与本监工无关"
        if self.adjudicated == 0 and self.false_negatives == 0:
            return "数据不足，继续攒"
        # hit_rate is None 且有漏报 = 从没报对过、却漏了东西，是「没干活」最强的情形。
        # 不能因为分母为 0 就落到「保留」。
        low = self.hit_rate is None or self.hit_rate < 0.3
        if low and self.false_negatives == 0:
            return "命中率低且无漏报 → 其管辖领域本无问题，降级为抽检"
        if low and self.false_negatives > 0:
            return "命中率低但有漏报 → 它没干活，重做提示词而非关掉"
        if self.cost_per_hit is not None and self.cost_per_hit > 1.0:
            return "命中率高但成本高 → 保留，考虑缩小输入范围降本"
        return "保留"


def supervisor_metrics(store, *, task_id: str | None = None) -> dict[str, SupervisorMetrics]:
    """按 role 聚合。task_id 为 None 时统计全库。

    从 store 读而不是自己开 Session：脱敏、加载策略都归 AuditStore 管，
    这里只做算术。
    """
    acc: dict[str, dict] = {}

    def bucket(role: str) -> dict:
        return acc.setdefault(
            role,
            dict(fired=0, passed=0, true_positives=0, false_positives=0,
                 unadjudicated=0, false_negatives=0, faults=0,
                 harness_faults=0, cost_usd=0.0, tokens=0),
        )

    for row in store.all_attempts(task_id=task_id):
        resolution = row.resolution
        has_defect = bool(row.linked_defects)
        for v in row.supervisors:
            b = bucket(str(v.role))
            b["cost_usd"] += v.cost_usd
            b["tokens"] += v.tokens
            if _is_harness_fault(v):
                # 上游/worker CLI 坏了。既不进 fired（不是告警），也不进 faults
                # （不是这个监工的毛病）—— 分开数的理由见 harness_faults。
                b["harness_faults"] += 1
            elif _is_fault(v):
                # 监工超时不是「它报了个警」。混进 fired 会污染命中率分母，
                # 而裁剪决定就建在那个分母上。
                b["faults"] += 1
            elif v.verdict == Verdict.FAIL:
                b["fired"] += 1
                if resolution in _TRUE_POSITIVE:
                    b["true_positives"] += 1
                elif resolution in _FALSE_POSITIVE:
                    b["false_positives"] += 1
                elif resolution in _UNADJUDICATED:
                    b["unadjudicated"] += 1
            else:
                b["passed"] += 1
                if has_defect:
                    b["false_negatives"] += 1

    return {role: SupervisorMetrics(role=role, **vals) for role, vals in acc.items()}


@dataclass(frozen=True)
class Gate3Rework:
    """spec §9 的 P1 判据：闸门 3 上人平均要打回几次才能验收通过。

    spec 同时说明这个指标检验的是**验收系统本身**而非 agent 能力：
    若每次都需人下去读代码找问题，说明问题在闸门 1（验收条件不够机器可判定）。
    """

    tasks: int = 0
    total_reworks: int = 0
    target: float = 1.0
    #: 因上游/worker CLI 报错（502 之类）而打回的轮次，**已从 total_reworks
    #: 里排掉**。单独留着是为了「排掉了多少」看得见 —— 悄悄排掉的话，一个
    #: 网关整天抽风的日子和一个真正顺畅的日子在这个指标上长得一样。
    upstream_reworks: int = 0

    @property
    def mean_reworks(self) -> float | None:
        if self.tasks == 0:
            return None
        return self.total_reworks / self.tasks

    @property
    def meets_p1_target(self) -> bool | None:
        """没数据返回 None —— 「没跑过任务」不等于「达标」。"""
        if self.mean_reworks is None:
            return None
        return self.mean_reworks <= self.target


def gate3_rework(store, *, target: float = 1.0) -> Gate3Rework:
    """按任务（不是按 attempt）统计打回次数。

    分母排除预分级就拦下、从未派发的任务：它们没进过闸门 3。
    不排除的话，C/D 类拦得越多平均打回次数越好看，指标会奖励错误的行为。

    **因上游报错（502）而打回的轮次也不算。** 这个指标检验的是验收系统够不够
    机器可判定 —— 一次网关抖动对那件事一个字都没说。不排掉的话，上游越不稳这
    个数越差，人会拿着它去改验收条件，而验收条件根本没问题。同一个形状在 P0
    判据上踩过一次（一次 400 让处理完全正确的链路判红）。

    排掉的数留在 `upstream_reworks` 里，不是丢掉：悄悄排掉的话，一个网关整天
    抽风的日子和一个真正顺畅的日子在这个指标上长得一样。
    """
    reworks: dict[str, int] = {}
    upstream = 0
    for row in store.all_attempts():
        if row.harness_version == NOT_DISPATCHED:
            continue
        reworks.setdefault(row.task_id, 0)
        if row.resolution == Resolution.REWORKED:
            if any(_is_harness_fault(v) for v in row.supervisors):
                upstream += 1
            else:
                reworks[row.task_id] += 1
    return Gate3Rework(
        tasks=len(reworks), total_reworks=sum(reworks.values()), target=target,
        upstream_reworks=upstream,
    )


# --------------------------------------------------------------- 端到端人时账
#
# P1 判据「闸门 3 上人平均打回次数 ≤ 1」量的是验收质量，答不了「这个需求让人
# 花了多少分钟」。没那个数，就没法证明这套东西省了时间，也没法判断下一步该往
# 哪投工（改闸门 1 的验收条件？还是加一道监工？）。
#
# 这里的所有分母都和上面一个规矩：量不到就 None，不是 0。「还没攒到数据」和
# 「人一分钟没花」两个结论差得远，混成一个数就没人会去区分了。


@dataclass(frozen=True)
class TaskHumanTime:
    """一个需求上的人时分布。"""

    task_id: str
    #: 闸门 1（人确认需求可判定）：confirm − blocked，按轮次累加。
    gate1_seconds: float | None = None
    #: 闸门 3（人验收交付）：override − resolved_at。
    gate3_seconds: float | None = None
    #: 从草稿第一次被拦到最后一轮判决落下。人时是它的一个子集。
    wall_clock_seconds: float | None = None
    #: 真的在等人（有 blocked 没 confirm / 判完了没人 override）。
    #: 和「量不到」分开：还在跑的 attempt 不算等人。
    gate1_pending: bool = False
    gate3_pending: bool = False
    #: 已经等了多久（此刻 − 开始等的时刻）。没在等就是 None，不是 0 ——
    #: 报 0 会和「刚刚才进队列」混在一起。
    #:
    #: **不进人时**：人还没来看，这段时间没人在花。混进去的话人时占比会随积压
    #: 一路涨过 100%，而那个数正是用来证明「无人」程度的。
    waiting_seconds: float | None = None

    @property
    def human_seconds(self) -> float | None:
        """两道闸门相加。一道量不到就只算另一道，两道都没有才 None。"""
        parts = [s for s in (self.gate1_seconds, self.gate3_seconds) if s is not None]
        return sum(parts) if parts else None

    @property
    def human_ratio(self) -> float | None:
        """人时占墙钟的比例 —— 「无人」到什么程度的直接读数。"""
        if self.human_seconds is None or not self.wall_clock_seconds:
            return None
        return self.human_seconds / self.wall_clock_seconds


@dataclass(frozen=True)
class HumanTimeLedger:
    """全库（或单个任务）的人时汇总。"""

    tasks: tuple[TaskHumanTime, ...] = ()
    #: 配不上对的端点数：只有 confirm 没有 blocked、override 早于 resolved_at。
    #: 留着而不是丢掉，和 Gate3Rework.upstream_reworks 一个道理 —— 悄悄丢掉的
    #: 话，一个记漏了一半的库和一个干净的库在这张表上长得一样。
    unpaired_events: int = 0

    def _measured(self, attr: str) -> list[float]:
        return [v for t in self.tasks if (v := getattr(t, attr)) is not None]

    def _total(self, attr: str) -> float | None:
        vals = self._measured(attr)
        return sum(vals) if vals else None

    def _mean(self, attr: str) -> float | None:
        vals = self._measured(attr)
        return sum(vals) / len(vals) if vals else None

    def _max(self, attr: str) -> float | None:
        vals = self._measured(attr)
        return max(vals) if vals else None

    @property
    def gate1_total(self) -> float | None:
        return self._total("gate1_seconds")

    @property
    def gate1_mean(self) -> float | None:
        return self._mean("gate1_seconds")

    @property
    def gate1_max(self) -> float | None:
        return self._max("gate1_seconds")

    @property
    def gate3_total(self) -> float | None:
        return self._total("gate3_seconds")

    @property
    def gate3_mean(self) -> float | None:
        return self._mean("gate3_seconds")

    @property
    def gate3_max(self) -> float | None:
        return self._max("gate3_seconds")

    @property
    def human_total(self) -> float | None:
        return self._total("human_seconds")

    @property
    def wall_clock_mean(self) -> float | None:
        return self._mean("wall_clock_seconds")

    @property
    def tasks_with_data(self) -> tuple[TaskHumanTime, ...]:
        """有话可说的那些 —— 量到了人时，或者真的在等人。

        一个刚派出去还没判的 attempt 会在 `tasks` 里留一行全是「—」的记录。
        那行在展示层是纯噪音，更糟的是它让「这库还没人时数据」的空态提示显示
        不出来，于是一张什么都没量到的表看起来像一张量过的表。

        `tasks` 保留全集：口径层不该替展示层做减法（按 task 查明细时，
        「这个任务确实一点人时都没有」本身是个答案）。
        """
        return tuple(
            t for t in self.tasks
            if t.human_seconds is not None or t.gate1_pending or t.gate3_pending
        )

    @property
    def _longest(self) -> TaskHumanTime | None:
        """积压里等最久的那个。取最长而不是合计 —— 10 个各等 1 分钟不是急事，
        1 个等了三天是。合计会把前者报成 10 分钟，把后者埋在平均数里。
        """
        waiting = [t for t in self.tasks if t.waiting_seconds is not None]
        if not waiting:
            return None
        return max(waiting, key=lambda t: (t.waiting_seconds, t.task_id))

    @property
    def longest_wait_seconds(self) -> float | None:
        t = self._longest
        return t.waiting_seconds if t is not None else None

    @property
    def longest_waiting_task(self) -> str | None:
        t = self._longest
        return t.task_id if t is not None else None

    @property
    def gate1_pending_tasks(self) -> int:
        return sum(1 for t in self.tasks if t.gate1_pending)

    @property
    def gate3_pending_tasks(self) -> int:
        return sum(1 for t in self.tasks if t.gate3_pending)


#: 这些判决意味着「机器交不出结论，躺在人的桌上」。其余的都不是人在等：
#: MERGED 是全绿自动落地（这套系统的常态），REWORKED 是 worker 自己会重跑，
#: PENDING 是还在跑。
_AWAITING_HUMAN = frozenset({Resolution.ESCALATED})


def _awaiting_human(attempts) -> datetime | None:
    """人是不是真的挂在这个任务上；是的话，从哪一刻开始等。

    判据取**最后一轮**的 resolution，不是「有没有后续 override 事件」。后者对
    自动落地的任务永远成立 —— 而自动落地正是这套系统的常态，于是那个读数会随
    系统跑得越顺涨得越高，最后没人会看它。
    """
    ruled = [a for a in attempts if a.resolved_at is not None]
    if not ruled:
        return None
    last = max(ruled, key=lambda a: (a.resolved_at, a.id))
    return last.resolved_at if last.resolution in _AWAITING_HUMAN else None


def _gate1_seconds(events) -> tuple[float | None, datetime | None, int]:
    """(用时, 从哪一刻起在等人, 配不上对的端点数)。

    第二个位置返回时刻而不是布尔：它既是「在不在等」也是「等了多久」的起点。
    只给布尔的话，等了三天的和刚进队列一分钟的在表上长得一样，而这张表是拿去
    做投工决定的。

    按轮次配对：blocked 开一段，下一个 confirm 关一段。累加而不是只取最后一段
    —— 一份草稿可以被拦两次（人补了一半又被拦），只算最后一次会低估人时。

    两个 blocked 之间的空档**不算**：人时是人花的时间，不是需求躺在队列里的
    时间。躺着那段归墙钟，混进来的话一个放了三天的需求会显示成三天人时。
    """
    total = 0.0
    counted = False
    unpaired = 0
    open_at = None
    for ev in events:
        if ev.gate != HumanGate.INTAKE:
            continue
        if ev.action == HumanAction.BLOCKED:
            # 连着两个 blocked（中间没人确认）：起点取后一个，前一个那段
            # 没有终点，不该凭空算出用时。
            if open_at is not None:
                unpaired += 1
            open_at = ev.created_at
        elif ev.action == HumanAction.CONFIRM:
            if open_at is None:
                # 人手写 YAML 直接塞 needs-human 再入队，就只有 confirm。
                # 和任何时刻相减都是编数。
                unpaired += 1
                continue
            total += (ev.created_at - open_at).total_seconds()
            counted = True
            open_at = None
    return (total if counted else None), open_at, unpaired


def _gate3_seconds(attempts, events) -> tuple[float | None, datetime | None, int]:
    """(用时, 从哪一刻起在等人, 配不上对的端点数)。

    起点是 resolved_at（判决落下 = 开始等人），不是派发时刻 —— 从派发算会把
    worker 干活的时间算成人时。终点是 override 事件。

    多轮打回时取「override 之前最后一个落了判决的 attempt」：人验收的是最后
    那一轮，取第一轮会把中间 worker 重跑的时间全算成人在看。
    """
    resolved = sorted(
        (a.resolved_at for a in attempts if a.resolved_at is not None),
    )
    overrides = [
        ev.created_at for ev in events
        if ev.gate == HumanGate.DELIVERY and ev.action == HumanAction.OVERRIDE
    ]
    if not resolved:
        # 还在跑，没人在等。这里必须是 None，否则积压数会把在跑的算进去。
        return None, None, len(overrides)

    # 一轮判决只收一次费：人把 override 跑了两遍（手滑 / 改主意）的话，每个
    # override 各减一次 resolved_at 会算出两倍人时。取最早那次定案。
    first_by_ruling: dict[datetime, datetime] = {}
    unpaired = 0
    for at in overrides:
        prior = [r for r in resolved if r <= at]
        if not prior:
            # override 早于任何判决 → 相减是负数。负人时是记错了，不是省下来的。
            unpaired += 1
            continue
        ruling = prior[-1]
        # 重复定案**不算** unpaired：那个计数器的含义是「有人绕过命令动了队列」，
        # 把正常的手滑混进去会让它报假警，于是真信号被埋掉。
        if ruling not in first_by_ruling or at < first_by_ruling[ruling]:
            first_by_ruling[ruling] = at

    waiting_from = _awaiting_human(attempts)
    # 只掐**这一轮**已经被定过案的情况。拿「这个任务有没有被定过案」当判据的话，
    # 人定完案 worker 又跑一轮又判不了（第二次上人）就永远看不见了。
    if waiting_from is not None and waiting_from in first_by_ruling:
        waiting_from = None
    if not first_by_ruling:
        return None, waiting_from, unpaired
    total = sum(
        (at - ruling).total_seconds() for ruling, at in first_by_ruling.items()
    )
    return total, waiting_from, unpaired


def human_time(
    store, *, task_id: str | None = None, now: datetime | None = None,
) -> HumanTimeLedger:
    """端到端人时账：闸门 1 用时、闸门 3 用时、需求→上线墙钟。

    `now` 可注入：算「已经等了多久」得有个此刻。用真时钟的话，测试只能断言
    `> 0`，而那个断言在起点取错的时候照样通过。

    数据源只有审计库一处（human_event + task_attempt.resolved_at）。刻意不读
    Journal：把一个度量建在「回读 JSONL 再拼接」上，就等于让它依赖文本解析。

    按人时降序返回，最费人的在最上面 —— 这张表要答的是「下一步该往哪投工」。
    """
    now = now or utc_now()
    events = store.human_events(task_id=task_id)
    by_task: dict[str, list] = {}
    for ev in events:
        by_task.setdefault(ev.task_id, []).append(ev)
    # 只有 attempt 没有 human_event 的任务也要在表上（它们可能正在等验收）。
    attempts_by_task: dict[str, list] = {}
    for row in store.all_attempts(task_id=task_id):
        attempts_by_task.setdefault(row.task_id, []).append(row)
        by_task.setdefault(row.task_id, [])

    rows = []
    unpaired = 0
    for tid, evs in by_task.items():
        attempts = attempts_by_task.get(tid, ())
        g1, g1_waiting_from, u1 = _gate1_seconds(evs)
        g3, g3_waiting_from, u3 = _gate3_seconds(attempts, evs)
        unpaired += u1 + u3
        # 两道闸门都在等的话取更早那个：这个任务被卡住的总时长是从第一次卡住算起。
        waits = [w for w in (g1_waiting_from, g3_waiting_from) if w is not None]
        waiting_seconds = (
            max(0.0, (now - min(waits)).total_seconds()) if waits else None
        )
        starts = [e.created_at for e in evs]
        resolved = [a.resolved_at for a in attempts if a.resolved_at is not None]
        # 终点要**同时**考虑判决时刻和人定案的时刻。只取 resolved_at 的话，
        # 闸门 3 那段人时整个落在窗口之外（override 永远晚于 resolved_at），
        # 于是人时占比能超过 100% —— 一个当场就说不通的数（人花的时间比这个
        # 需求存在的时间还长）。实测跑出来就是 109.8%。
        ends = resolved + [
            e.created_at for e in evs
            if e.gate == HumanGate.DELIVERY and e.action == HumanAction.OVERRIDE
        ]
        # 但「落地了」的判据仍然只看 resolved_at：一个只有人时事件、从没被判过
        # 的需求没有上线时刻，给它一个墙钟等于说它交付了。
        if not resolved:
            ends = []
        rows.append(TaskHumanTime(
            task_id=tid, gate1_seconds=g1, gate3_seconds=g3,
            # 墙钟要两端都在：只有起点说明还没落地，给个数就等于说它上线了。
            wall_clock_seconds=(
                (max(ends) - min(starts)).total_seconds()
                if starts and ends else None
            ),
            gate1_pending=g1_waiting_from is not None,
            gate3_pending=g3_waiting_from is not None,
            waiting_seconds=waiting_seconds,
        ))
    rows.sort(key=lambda t: (-(t.human_seconds or 0.0), t.task_id))
    return HumanTimeLedger(tasks=tuple(rows), unpaired_events=unpaired)
