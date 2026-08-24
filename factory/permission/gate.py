"""权限门入口：把三层串起来。对外只暴露 check_tool_call。

分层顺序和短路逻辑：

    静态规则 DENY   → 直接拦，不花钱、不等待
    静态规则 ALLOW  → 直接过，不花钱、不等待     ← 绝大多数动作走这条
    静态规则 ESCALATE → 问 haiku
        审批者 ALLOW/DENY → 照办
        审批者 不可用     → ESCALATE 给上层按风险决定

**成本模型决定了这个顺序不能反。** 一次派发几百个工具调用，如果先问模型
再看规则，光审批费就能超过任务本身。静态规则的存在意义就是把 99% 的动作
在零成本处解决掉。

审计：每次非 ALLOW 的判决都要能在事后查到「谁拦的、为什么、花了多少」。
GateEvent 就是那条记录，由调用方落库——这个模块不碰数据库，保持可单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.permission.approver import ModelApprover, ruling_from_approval
from factory.permission.rules import Decision, Ruling, check_command, check_path

#: 哪些工具的哪个参数是要审的目标。
#:
#: 只列**会改变世界**的工具。Read/Grep/Glob 这些纯读操作不进门：
#: 它们改不了任何东西，而每次多一道检查就是一次延迟。沙箱已经管住了
#: 「能读到哪」，这道门管的是「能改什么」。
_WRITE_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}

_COMMAND_TOOLS: dict[str, str] = {
    "Bash": "command",
}


@dataclass
class GateEvent:
    """一次权限判决的审计记录。"""

    tool: str
    target: str
    decision: Decision
    rule: str
    reason: str
    #: 走了模型审批的话记成本，静态规则判的是 0。
    cost_usd: float = 0.0
    tokens: int = 0


@dataclass
class GateStats:
    """一次派发里权限门的累计情况。进报表用。"""

    checked: int = 0
    allowed: int = 0
    denied: int = 0
    escalated: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    events: list[GateEvent] = field(default_factory=list)

    def record(self, ev: GateEvent, *, used_model: bool) -> None:
        self.checked += 1
        if ev.decision == Decision.ALLOW:
            self.allowed += 1
        elif ev.decision == Decision.DENY:
            self.denied += 1
        else:
            self.escalated += 1
        if used_model:
            self.model_calls += 1
            self.cost_usd += ev.cost_usd
        # 只留非 ALLOW 的事件：一次派发几百个 allow 全存下来，
        # 审计库会被噪声灌满，而没人会去看「哪些动作被放行了」。
        if ev.decision != Decision.ALLOW:
            self.events.append(ev)


class PermissionGate:
    """三层权限门。

    approver=None 表示不启用第 2 层：静态规则 ESCALATE 时直接返回 ESCALATE。
    用于测试和「先只上静态规则」的渐进部署——这道门第一次上线时，
    先跑一段只有静态规则的模式，看 escalate 率再决定要不要接模型，
    比一次性全开更容易发现规则写得对不对。
    """

    def __init__(self, approver: ModelApprover | None = None) -> None:
        self._approver = approver
        self.stats = GateStats()

    def check_tool_call(self, tool: str, args: dict) -> Ruling:
        """一个工具调用该不该放行。

        不认识的工具直接 ALLOW。理由：白名单式拦截（不认识就拦）在这里是
        错的——claude CLI 的工具集会随版本增加，一个新工具出现就让整条
        流水线停摆，而它很可能完全无害（比如新的搜索工具）。
        真正的边界防护是沙箱，这道门只补沙箱管不到的那部分。
        """
        if (key := _WRITE_TOOLS.get(tool)):
            target = str(args.get(key, ""))
            ruling = check_path(target)
            return self._finish(tool, target, ruling, payload=target)

        if (key := _COMMAND_TOOLS.get(tool)):
            target = str(args.get(key, ""))
            ruling = check_command(target)
            return self._finish(tool, target, ruling, payload=target)

        return Ruling(Decision.ALLOW)

    def _finish(
        self, tool: str, target: str, ruling: Ruling, *, payload: str
    ) -> Ruling:
        """静态规则出结果后的处理：该问模型的问，然后记账。"""
        used_model = False
        cost = 0.0
        tokens = 0

        if ruling.decision == Decision.ESCALATE and self._approver is not None:
            approval = self._approver.review(tool, payload)
            ruling = ruling_from_approval(approval)
            used_model = True
            cost = approval.cost_usd
            tokens = approval.tokens

        self.stats.record(
            GateEvent(
                tool=tool,
                target=target[:500],
                decision=ruling.decision,
                rule=ruling.rule,
                reason=ruling.reason,
                cost_usd=cost,
                tokens=tokens,
            ),
            used_model=used_model,
        )
        return ruling
