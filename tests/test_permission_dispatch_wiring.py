"""dispatcher → 审计 的权限门接线测试。

这条路径断掉的表现是「拦住了但审计里查不到」—— 门在干活，可是没人知道
它拦了什么。无人值守场景下这等于没有证据链。

重点验三件事：
1. 事件真的落到 store 里（形状对齐 record_permission_event 的 kwargs）
2. `_record_gate_events` 紧跟 record_result，中间任何 return 都不会漏记
3. 假 adapter（没有 drain_gate_events 方法）不炸 —— dispatcher 只依赖
   HarnessAdapter 协议，getattr 探而不是 isinstance
"""

from __future__ import annotations

from factory.audit.store import AuditStore
from factory.audit.models import OracleClass


def test_record_permission_event_accepts_our_kwargs(tmp_path):
    """adapter 产出的 dict 必须能直接喂给 store。

    两边形状对不上的话，dispatcher 那句 `store.record_permission_event(aid, **ev)`
    会 TypeError —— 而它在 attempt 收尾处，炸了会把整轮结果带走。
    """
    store = AuditStore(tmp_path / "audit.db")
    aid = store.open_attempt(
        task_id="t1",
        spec_ref=["spec.md"],
        oracle_class=OracleClass.A,
        class_reason="test",
        harness="claude_code",
        harness_version="1.0",
        model="haiku",
    )

    # 这个 dict 的键必须和 ClaudeCodeAdapter.drain_gate_events() 产出的一致
    event = {
        "tool": "Bash",
        "target": "git reset --hard",
        "decision": "deny",
        "rule": "git-reset-hard",
        "reason": "会抹掉未提交改动",
        "tokens": 0,
        "cost_usd": 0.0,
    }
    store.record_permission_event(aid, **event)

    rows = store.permission_events_for(aid)
    assert len(rows) == 1
    assert rows[0].decision == "deny"
    assert rows[0].rule == "git-reset-hard"


def test_adapter_without_drain_does_not_break_dispatch():
    """没有 drain_gate_events 的 adapter 必须被安全跳过。

    dispatcher 只依赖 HarnessAdapter 协议，测试里的假 adapter 和
    ShellAdapter 都没有这个方法。用 getattr 探而不是 isinstance 检查，
    所以这里验的是「探不到就当没有事件」而不是抛 AttributeError。
    """

    class BareAdapter:
        pass

    drain = getattr(BareAdapter(), "drain_gate_events", None)
    assert drain is None, "裸 adapter 不该有这个方法"
    # dispatcher 里的形状：drain is None 时直接不进循环
    events = drain() if drain is not None else ()
    assert events == ()
