"""任务定义。P0 从 YAML 读，P1 才由 PRD 自动生成。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from factory.spec_doc import Resolved, resolve


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

    **spec_doc 是 spec_ref 的必要搭档。** 光有编号等于没有标准：监工拿到的
    字面就是 `AC-1` 这四个字符。实测过一次 —— 监工自己看出来了，但它判 fail，
    那条 claim 不带 supervisor- 前缀，于是被当成真问题打回 worker，
    而 worker 改不了「AC-1 没有正文」。所以 spec_ref 非空时 spec_doc 必填，
    由 dispatcher 在派发前拦（见 factory/spec_doc.py）。
    """

    task_id: str
    prompt: str
    spec_ref: tuple[str, ...] = ()
    #: 规格文档路径，相对 workspace 根。spec_ref 非空时必填。
    spec_doc: str = ""
    acceptance: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    declared_ops: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    max_rounds: int = 3
    #: 必须先合并（进 done/）才能认领本任务的前置。填的是**队列里的文件名
    #: stem**，不是 YAML 里的 task_id —— 队列层从头到尾用文件名当身份
    #: （见 Claim.task_id），两套身份混用会让依赖在「文件名和 task_id 不一致」
    #: 时静默永不满足。判满足的是 backlog，dispatcher 不看这个字段。
    depends_on: tuple[str, ...] = ()

    def resolve_spec(self, root: str | Path | None = None) -> Resolved:
        """把 spec_ref 的编号解析成规格文档里的正文。"""
        return resolve(self.spec_ref, self.spec_doc or None, root=root)

    def criteria(self, root: str | Path | None = None) -> tuple[str, ...]:
        """递给规格监工的验收标准。**解析后的正文** + 本任务写明的。

        原来这是个 property，返回 `spec_ref + acceptance` —— 编号原样进 prompt。
        改成方法是因为解析需要知道仓库根：编号本身不是标准，正文才是。

        解析失败时这里返回的是「能解析出来的那些」，**不抛**。判断该不该派发
        是 dispatcher 的事（它要记审计、要走 escalated），这个方法只负责
        「有什么就给什么」—— 让它抛会把一个策略决定藏进数据访问里。
        """
        return self.resolve_spec(root).bodies + self.acceptance

    @classmethod
    def from_yaml(cls, path: str | Path) -> Task:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_mapping(doc)

    @classmethod
    def from_mapping(cls, doc: dict) -> Task:
        """从一个已经 safe_load 过的 mapping 构造 Task。

        从 `from_yaml` 里抽出来，为的是让「不落盘的 YAML」（`/api/submit`
        收到的请求体）能走**完全同一套**字段规则。分成两份实现的代价是
        实测过的：投递闸门放行的形状和派发时实际解析的形状一旦分叉，
        坏任务就在凌晨认领时才炸。

        字段缺失/类型不对时**照常抛**（KeyError / TypeError / ValueError）——
        调用方决定那是 400 还是崩，这一层不替它判。
        """
        if not isinstance(doc, dict):
            raise TypeError(
                f"任务 YAML 顶层必须是键值对，当前是 {type(doc).__name__}"
            )
        return cls(
            task_id=doc["task_id"],
            prompt=doc["prompt"],
            spec_ref=tuple(doc.get("spec_ref", ())),
            spec_doc=str(doc.get("spec_doc", "") or ""),
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
            depends_on=tuple(str(d) for d in (doc.get("depends_on") or ())),
        )
