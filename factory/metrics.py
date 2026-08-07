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

from factory.audit.models import Resolution, Verdict

# FAIL 之后，这些 resolution 说明告警是真的 / 是假的 / 还没判
_TRUE_POSITIVE = {Resolution.REWORKED}
_FALSE_POSITIVE = {Resolution.HUMAN_OVERRIDE, Resolution.MERGED}
_UNADJUDICATED = {Resolution.ESCALATED, Resolution.PENDING}


@dataclass(frozen=True)
class SupervisorMetrics:
    role: str
    fired: int = 0              # 报 FAIL 的次数
    passed: int = 0             # 报 PASS 的次数
    true_positives: int = 0
    false_positives: int = 0
    unadjudicated: int = 0      # 报了 FAIL 但人还没定案
    false_negatives: int = 0    # 报 PASS 却事后挂上 defect
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
                 unadjudicated=0, false_negatives=0, cost_usd=0.0, tokens=0),
        )

    for row in store.all_attempts(task_id=task_id):
        resolution = row.resolution
        has_defect = bool(row.linked_defects)
        for v in row.supervisors:
            b = bucket(str(v.role))
            b["cost_usd"] += v.cost_usd
            b["tokens"] += v.tokens
            if v.verdict == Verdict.FAIL:
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
