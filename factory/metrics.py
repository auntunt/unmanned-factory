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

from factory.audit.models import NOT_DISPATCHED, Resolution, Verdict
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
class GateMetrics:
    """单道闸门的表现（§9.2）。

    为什么 role 层不够：13 道硬闸门全报 `role='risk'`，按 role 聚合等于把
    13 件事的命中率搅成一个数。一道从不触发的闸门（写错了判据、或者它防的
    那类问题根本不存在）在 role 表上完全看不出来 —— 它的 0 次触发被另外
    12 道的命中稀释掉了。改进决策要的是「哪一道该删、哪一道该修」，那必须
    按闸门看。

    身份取 claim 的 `check` 字段，不新增数据库列：dashboard 的 GATE_CLAIMS /
    FAULT_CLAIMS 已经用同一套读法，加列等于把同一个事实存两遍，然后等它们
    不一致。
    """

    role: str
    gate: str
    fired: int = 0
    true_positives: int = 0
    false_positives: int = 0
    unadjudicated: int = 0
    cost_usd: float = 0.0

    @property
    def gate_id(self) -> str:
        """`risk:fake-green` 这种全限定名，报表和日志里用。"""
        return f"{self.role}:{self.gate}"

    @property
    def adjudicated(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def hit_rate(self) -> float | None:
        if self.adjudicated == 0:
            return None
        return self.true_positives / self.adjudicated

    def verdict_line(self) -> str:
        """这一道闸门该留、该修、还是该删。"""
        if self.fired == 0:
            # 从不触发有两种成因，数据分不开，所以只说事实和该干什么。
            return "从未触发 → 要么判据写错了，要么它防的问题不存在；查一次"
        if self.adjudicated == 0:
            return "触发过但无定案 → 先把这些 attempt 判了"
        if self.hit_rate is not None and self.hit_rate < 0.3:
            return "多数是误报 → 收紧判据，否则它在训练大家忽略告警"
        return "保留"


#: 硬闸门都挂在这个 role 下（dispatcher 的 _blocked() 全部走 RISK）。
#: gate_metrics 补零时需要它来拼 key。
RISK_ROLE = "risk"


def gate_metrics(
    store, *, task_id: str | None = None, known_gates: dict[str, str] | None = None
) -> dict[str, GateMetrics]:
    """按 (role, claim 的 check 名) 二级聚合，键是 `role:gate`（§9.2）。

    `known_gates` 传闸门名清单（dashboard.GATE_CLAIMS）时，**没触发过的闸门
    也会出现在结果里**，fired=0。这是这个函数存在的主要理由：一道从不触发的
    闸门只有在报表上占一行、写着「从未触发」，才会有人去查它是不是写坏了。
    只统计出现过的 claim 等于让坏掉的闸门继续隐身。

    只数 FAIL：PASS 的裁决没有 claims（一道没触发的闸门不写 claim），
    passed / false_negatives 那两列在闸门粒度上没有对应数据，硬凑会得出
    「每道闸门都漏报了 N 次」这种假账。漏报是 role 层的指标。
    """
    acc: dict[tuple[str, str], dict] = {}

    def bucket(role: str, gate: str) -> dict:
        return acc.setdefault(
            (role, gate),
            dict(fired=0, true_positives=0, false_positives=0,
                 unadjudicated=0, cost_usd=0.0),
        )

    for row in store.all_attempts(task_id=task_id):
        resolution = row.resolution
        for v in row.supervisors:
            if v.verdict != Verdict.FAIL:
                continue
            role = str(v.role)
            for c in v.claims:
                gate = str(c.get("check", "")) or "(未命名)"
                b = bucket(role, gate)
                b["fired"] += 1
                if resolution in _TRUE_POSITIVE:
                    b["true_positives"] += 1
                elif resolution in _FALSE_POSITIVE:
                    b["false_positives"] += 1
                elif resolution in _UNADJUDICATED:
                    b["unadjudicated"] += 1
            # 成本记在裁决上而不是逐条 claim 上：一次监工调用出 3 条 claim 时
            # 逐条累加会把成本算成 3 倍。挂在第一条 claim 的闸门上也不对
            # （它没多花钱），所以按 role 平摊由调用方决定，这里不摊。

    out = {
        f"{role}:{gate}": GateMetrics(role=role, gate=gate, **vals)
        for (role, gate), vals in acc.items()
    }
    # 补零：没触发过的闸门也要占一行
    for gate in known_gates or {}:
        key = f"{RISK_ROLE}:{gate}"
        out.setdefault(key, GateMetrics(role=RISK_ROLE, gate=gate))
    return out


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
