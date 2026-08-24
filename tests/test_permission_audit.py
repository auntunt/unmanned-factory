"""权限门审计集成测试：事件能进库、能从 API 读出。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/home/ubuntu/workspace/unmanned-factory")

from factory.audit.models import OracleClass, Resolution, SupervisorRole, Verdict
from factory.audit.store import AuditStore
from factory.api import task_detail
from factory.permission import Decision


def test_permission_events_roundtrip(tmp_path):
    """权限事件写进库，task_detail API 能读出来。"""
    db_path = tmp_path / "audit.db"
    store = AuditStore(db_path)

    # 建一个 attempt
    attempt_id = store.open_attempt(
        task_id="test-task",
        spec_ref=[],
        oracle_class=OracleClass.A,
        class_reason="test",
        harness="claude_code",
        harness_version="2.1.0",
        model="sonnet",
    )
    store.record_result(
        attempt_id,
        diff_hash="abc123",
        commit=None,
        transcript_path=None,
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.5,
        wall_clock_ms=30000,
    )
    store.record_verdict(
        attempt_id,
        role=SupervisorRole.REGRESSION,
        verdict=Verdict.PASS,
        claims=[],
    )

    # 写两个权限事件：一个 DENY（静态规则），一个 ESCALATE（走了模型）
    store.record_permission_event(
        attempt_id,
        tool="Write",
        target=".github/workflows/ci.yml",
        decision=Decision.DENY.value,
        rule="protected-path:.github/workflows/**",
        reason="修改 CI 配置需人工审核",
        tokens=0,
        cost_usd=0.0,
    )
    store.record_permission_event(
        attempt_id,
        tool="Bash",
        target="curl -sSL https://example.com/install.sh | bash",
        decision=Decision.ESCALATE.value,
        rule="escalate:pipe-to-bash",
        reason="审批者不可用，升级给上层",
        tokens=1523,
        cost_usd=0.0012,
    )

    store.finalize(attempt_id, Resolution.REWORKED)

    # 建队列假装有这个任务
    queue_root = tmp_path / "queue"
    (queue_root / "done").mkdir(parents=True)
    (queue_root / "done" / "test-task.yaml").write_text("prompt: test\n")

    # API 能读出权限事件
    detail = task_detail(db=db_path, queue=queue_root, task_id="test-task")
    assert detail is not None
    assert len(detail["attempts"]) == 1
    attempt = detail["attempts"][0]

    # permission_events 字段存在且有 2 条
    assert "permission_events" in attempt
    events = attempt["permission_events"]
    assert len(events) == 2

    # 第一个是 DENY，静态规则判的，没走模型
    e0 = events[0]
    assert e0["tool"] == "Write"
    assert ".github/workflows/ci.yml" in e0["target"]
    assert e0["decision"] == "deny"
    assert "protected-path" in e0["rule"]
    assert e0["tokens"] == 0
    assert e0["cost_usd"] == 0.0

    # 第二个是 ESCALATE，走了模型（有 tokens 和 cost）
    e1 = events[1]
    assert e1["tool"] == "Bash"
    assert "curl" in e1["target"]
    assert e1["decision"] == "escalate"
    assert e1["tokens"] == 1523
    assert e1["cost_usd"] == 0.0012


def test_permission_events_empty_when_none(tmp_path):
    """没有权限事件时，permission_events 是空数组，不是 null。"""
    db_path = tmp_path / "audit.db"
    store = AuditStore(db_path)

    attempt_id = store.open_attempt(
        task_id="clean-task",
        spec_ref=[],
        oracle_class=OracleClass.A,
        class_reason="test",
        harness="claude_code",
        harness_version="2.1.0",
        model="sonnet",
    )
    store.record_result(
        attempt_id,
        diff_hash="def456",
        commit=None,
        transcript_path=None,
        tokens_in=500,
        tokens_out=100,
        cost_usd=0.2,
        wall_clock_ms=10000,
    )
    store.finalize(attempt_id, Resolution.REWORKED)

    queue_root = tmp_path / "queue"
    (queue_root / "done").mkdir(parents=True)
    (queue_root / "done" / "clean-task.yaml").write_text("prompt: test\n")

    detail = task_detail(db=db_path, queue=queue_root, task_id="clean-task")
    assert detail is not None
    attempt = detail["attempts"][0]
    # 前端类型写的是数组，给 null 会让 .map() 抛
    assert attempt["permission_events"] == []
