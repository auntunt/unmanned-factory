"""静态分级规则引擎。spec §3 按裁判成本分级。

这台引擎在一次 attempt 里跑两次：
  1. 派发前，用任务声明的 declared_paths / declared_ops → 决定要不要无人
  2. 拿到 diff 后，用真实改动的文件 → 比第一次严重就升级

spec §4 里的「风险监工」就是第二次调用，不是独立组件。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml

from factory.audit.models import OracleClass

SEVERITY: dict[OracleClass, int] = {
    OracleClass.A: 0,
    OracleClass.B: 1,
    OracleClass.C: 2,
    OracleClass.D: 3,
}

DEFAULT_REASON = "no rule matched -> default A"


@dataclass(frozen=True)
class Rule:
    oracle_class: OracleClass
    reason: str
    patterns: tuple[str, ...] = ()
    ops: tuple[str, ...] = ()

    def match(self, paths: list[str], ops: list[str]) -> tuple[str, ...]:
        hits: list[str] = [
            p for p in paths if any(fnmatch(p, pat) for pat in self.patterns)
        ]
        hits += [o for o in ops if o in self.ops]
        return tuple(dict.fromkeys(hits))  # 去重且保序


@dataclass(frozen=True)
class Grade:
    oracle_class: OracleClass
    reason: str
    triggers: tuple[str, ...] = ()

    @property
    def unmanned_allowed(self) -> bool:
        """只有 A/B 允许无人。C 永不无人，D 是硬闸门。"""
        return self.oracle_class in (OracleClass.A, OracleClass.B)

    @property
    def hard_gate(self) -> bool:
        return self.oracle_class == OracleClass.D

    def more_severe_than(self, other: Grade) -> bool:
        return SEVERITY[self.oracle_class] > SEVERITY[other.oracle_class]


class GradingEngine:
    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def from_yaml(cls, path: str | Path) -> GradingEngine:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            [
                Rule(
                    oracle_class=OracleClass(r["class"]),
                    reason=r["reason"],
                    patterns=tuple(r.get("patterns", ())),
                    ops=tuple(r.get("ops", ())),
                )
                for r in doc.get("rules", [])
            ]
        )

    @classmethod
    def default(cls) -> GradingEngine:
        return cls.from_yaml(Path(__file__).with_name("oracle_rules.yaml"))

    def grade(self, paths: Iterable[str] = (), ops: Iterable[str] = ()) -> Grade:
        path_list, op_list = list(paths), list(ops)
        worst: Grade | None = None
        for rule in self._rules:
            hits = rule.match(path_list, op_list)
            if not hits:
                continue
            candidate = Grade(
                oracle_class=rule.oracle_class,
                reason=f"{rule.reason} [{', '.join(hits)}]",
                triggers=hits,
            )
            if worst is None or candidate.more_severe_than(worst):
                worst = candidate
        return worst or Grade(OracleClass.A, DEFAULT_REASON, ())
