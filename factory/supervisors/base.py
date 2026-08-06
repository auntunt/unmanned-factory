"""监工层公共类型。spec §4：三个出证据，一个出意见。

P0 只有回归监工（出证据、确定性、零模型成本）。
规格/架构监工在 P1，那时 tokens/cost_usd 才会非 0。
"""

from __future__ import annotations

from dataclasses import dataclass

from factory.audit.models import SupervisorRole, Verdict


@dataclass(frozen=True)
class SupervisorReport:
    role: SupervisorRole
    verdict: Verdict
    claims: tuple[dict, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS
