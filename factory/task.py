"""任务定义。P0 从 YAML 读，P1 才由 PRD 自动生成。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CheckSpec:
    """回归监工的一条检查。确定性执行，不调模型。

    expect:
      exit_zero        —— command 退出码为 0
      stdout_contains  —— command 的 stdout 含 value
      commands_agree   —— command 与 value 两条命令的 stdout 完全一致
                          （对应部署 runbook 里「本地镜像 ID == 容器镜像 ID」这类核对）
    """

    name: str
    command: str
    expect: str = "exit_zero"
    value: str = ""
    timeout_s: int = 300


@dataclass(frozen=True)
class Task:
    task_id: str
    prompt: str
    spec_ref: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    declared_ops: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    max_rounds: int = 3

    @classmethod
    def from_yaml(cls, path: str | Path) -> Task:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            task_id=doc["task_id"],
            prompt=doc["prompt"],
            spec_ref=tuple(doc.get("spec_ref", ())),
            declared_paths=tuple(doc.get("declared_paths", ())),
            declared_ops=tuple(doc.get("declared_ops", ())),
            checks=tuple(
                CheckSpec(
                    name=c["name"],
                    command=c["command"],
                    expect=c.get("expect", "exit_zero"),
                    value=c.get("value", ""),
                    timeout_s=int(c.get("timeout_s", 300)),
                )
                for c in doc.get("checks", ())
            ),
            max_rounds=int(doc.get("max_rounds", 3)),
        )
