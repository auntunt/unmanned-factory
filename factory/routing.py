"""模型分级路由。第一轴是模型档位，第二轴（换 harness）留给 P1。"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.audit.models import OracleClass


class Router:
    def __init__(self, table: dict[str, list[str]], default: list[str]) -> None:
        self._table = {str(k): list(v) for k, v in (table or {}).items()}
        self._default = list(default) or ["sonnet"]

    @classmethod
    def from_yaml(cls, path: str | Path) -> Router:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(doc.get("by_class", {}), doc.get("default", ["sonnet"]))

    @classmethod
    def default(cls) -> Router:
        return cls.from_yaml(Path(__file__).with_name("routing.yaml"))

    def model_for(self, oracle_class: OracleClass, attempt_no: int) -> str:
        """attempt_no 从 1 起。超出阶梯长度则钉在最后一档（最强的那个）。"""
        ladder = self._table.get(str(oracle_class.value), self._default)
        idx = max(0, min(attempt_no - 1, len(ladder) - 1))
        return ladder[idx]
