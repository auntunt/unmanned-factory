"""权限门：静态规则 → 廉价模型审批者 → 升级给人。

对外入口是 PermissionGate.check_tool_call(tool, args) → Ruling。
分层理由见 gate.py 的模块 docstring；每层的判据分别在 rules.py / approver.py。
"""

from factory.permission.approver import Approval, ModelApprover
from factory.permission.gate import GateEvent, GateStats, PermissionGate
from factory.permission.rules import Decision, Ruling, check_command, check_path

__all__ = [
    "Approval",
    "Decision",
    "GateEvent",
    "GateStats",
    "ModelApprover",
    "PermissionGate",
    "Ruling",
    "check_command",
    "check_path",
]
