"""Project dollar-budget decisions shared by provider dispatch boundaries."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


class BudgetConfigurationError(ValueError):
    pass


def _amount(value: Any, label: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise BudgetConfigurationError(f"{label} must be a finite non-negative number")
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BudgetConfigurationError(f"{label} must be a finite non-negative number") from None
    if not math.isfinite(amount) or amount < 0:
        raise BudgetConfigurationError(f"{label} must be a finite non-negative number")
    return amount


@dataclass(frozen=True)
class DollarBudget:
    """A snapshot of durable run usage against one project-run limit."""

    limit_usd: float | None
    known_cost_usd: float
    unknown_cost_calls: int

    @property
    def remaining_usd(self) -> float | None:
        if self.limit_usd is None:
            return None
        return max(0.0, self.limit_usd - self.known_cost_usd)

    @property
    def exhausted(self) -> bool:
        return self.limit_usd is not None and self.known_cost_usd >= self.limit_usd

    def block_reason(self, *, unknown_cost_policy: str = "allow_bounded") -> str | None:
        if unknown_cost_policy not in ("stop", "allow_bounded"):
            raise BudgetConfigurationError("unknown_cost_policy must be stop or allow_bounded")
        if self.unknown_cost_calls and unknown_cost_policy == "stop":
            return "provider cost is unknown; continuation requires human review"
        if self.exhausted:
            return (f"known provider cost ${self.known_cost_usd:.4f} exhausted budget "
                    f"${self.limit_usd:.4f}")
        return None


def dollar_budget(limit_usd: Any, usage: Mapping[str, Any]) -> DollarBudget:
    """Validate and normalize an event-derived usage subtotal.

    ``None`` keeps the executor's explicit unlimited mode for non-project
    callers. Project service paths always supply their configured finite limit.
    """
    if not isinstance(usage, Mapping):
        raise BudgetConfigurationError("usage must be a mapping")
    limit = _amount(limit_usd, "budget_usd", optional=True)
    known = _amount(usage.get("known_cost_usd", 0.0), "known_cost_usd")
    unknown = usage.get("unknown_cost_calls", 0)
    if type(unknown) is not int or unknown < 0:
        raise BudgetConfigurationError("unknown_cost_calls must be a non-negative integer")
    return DollarBudget(limit_usd=limit, known_cost_usd=known, unknown_cost_calls=unknown)
