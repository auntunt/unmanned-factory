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
    """一条待派发任务。

    spec_ref 和 acceptance 是两件事，别合并：
      spec_ref    引用**外部已有**规格文档的编号（AC-1、§3.2）
      acceptance  写在本任务里的验收标准原文

    入口层（`factory prd`）逼出了这个区分。一句口述需求没有外部文档可引，
    它本身就是规格 —— 只有 spec_ref 的话，所有口述来源的任务都会因为
    「没有验收标准」被规格监工永久判 fail，而那不是任务的错。
    规格监工拿的是两者的并集。
    """

    task_id: str
    prompt: str
    spec_ref: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    declared_ops: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    max_rounds: int = 3

    @property
    def criteria(self) -> tuple[str, ...]:
        """递给规格监工的验收标准。外部引用 + 本任务写明的，取并集。"""
        return self.spec_ref + self.acceptance

    @classmethod
    def from_yaml(cls, path: str | Path) -> Task:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            task_id=doc["task_id"],
            prompt=doc["prompt"],
            spec_ref=tuple(doc.get("spec_ref", ())),
            acceptance=tuple(doc.get("acceptance", ())),
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
