"""权限门集成测试：三层一起跑，验证短路和成本。"""
import sys

import pytest

sys.path.insert(0, "/home/ubuntu/workspace/unmanned-factory")

from factory.permission import Decision, PermissionGate


def test_write_protected_path_denied_by_rules():
    """写受保护路径被静态规则拦，不走模型。"""
    gate = PermissionGate(approver=None)
    r = gate.check_tool_call("Write", {"file_path": ".github/workflows/ci.yml"})
    assert r.decision == Decision.DENY
    assert r.rule.startswith("protected-path:")
    assert gate.stats.denied == 1
    assert gate.stats.model_calls == 0


def test_bash_dangerous_denied_by_rules():
    """危险 shell 命令被静态规则拦，不走模型。"""
    gate = PermissionGate(approver=None)
    r = gate.check_tool_call("Bash", {"command": "git reset --hard HEAD"})
    assert r.decision == Decision.DENY
    assert r.rule == "git-reset-hard"
    assert gate.stats.denied == 1
    assert gate.stats.model_calls == 0


def test_safe_write_allowed_without_model():
    """普通写操作静态规则直接放行，不走模型。"""
    gate = PermissionGate(approver=None)
    r = gate.check_tool_call("Write", {"file_path": "src/main.py"})
    assert r.decision == Decision.ALLOW
    assert gate.stats.allowed == 1
    assert gate.stats.model_calls == 0


def test_read_tool_not_checked():
    """只读工具（Read/Grep）不进门，直接放行。"""
    gate = PermissionGate(approver=None)
    r = gate.check_tool_call("Read", {"file_path": "anything"})
    assert r.decision == Decision.ALLOW
    assert gate.stats.checked == 0  # 没走 _finish


def test_unknown_tool_allowed():
    """不认识的工具直接放行，不拦。"""
    gate = PermissionGate(approver=None)
    r = gate.check_tool_call("FutureTool", {"arg": "value"})
    assert r.decision == Decision.ALLOW


def test_stats_accumulate():
    """stats 能累计多次调用。"""
    gate = PermissionGate(approver=None)
    gate.check_tool_call("Write", {"file_path": "a.py"})  # allow
    gate.check_tool_call("Write", {"file_path": "b.py"})  # allow
    gate.check_tool_call("Write", {"file_path": ".github/workflows/x.yml"})  # deny
    gate.check_tool_call("Bash", {"command": "git push --force"})  # deny

    assert gate.stats.checked == 4
    assert gate.stats.allowed == 2
    assert gate.stats.denied == 2
    # 只有 denied 的 event 被记下来（allow 不记，否则噪声太多）
    assert len(gate.stats.events) == 2
    assert all(e.decision == Decision.DENY for e in gate.stats.events)


def test_approver_none_escalate_stays_escalate():
    """approver=None 时，静态规则 ESCALATE 的直接返回 ESCALATE。"""
    gate = PermissionGate(approver=None)
    # 目前静态规则全是 ALLOW/DENY，没有 ESCALATE，这条测试先写着
    # 预留给以后规则里可能出现的判不出的情况。
    # 如果 check_command 改成在含糊时返回 ESCALATE，这条就能跑了。
    assert gate._approver is None


# 有 approver 的测试需要真调 claude，成本和延迟都高，先不写在单测里。
# 如果要验证模型层，可以写一个 fake approver（ok=True, decision=allow）注入。
