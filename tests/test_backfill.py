"""事后回填。spec §5：resolution 与 linked_defects 此字段事后填写。

P0 只有流水线自己写 resolution，人没有入口。缺了人工回填，两周后
只有「监工说 fail」的计数，算不出命中率 —— 所以回填入口和报表一样承重。
"""
import pytest

from factory.audit.models import (
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.audit.store import AuditStore
from factory.cli import main


@pytest.fixture
def db(tmp_path):
    """一个已 escalated 的 attempt，等人定案。"""
    path = tmp_path / "audit.db"
    s = AuditStore(path)
    aid = s.open_attempt(
        task_id="T-bf", spec_ref=["AC-1"], oracle_class=OracleClass.A,
        class_reason="A", harness="claude_code", harness_version="2.1",
        model="haiku",
    )
    s.record_verdict(aid, role=SupervisorRole.SPEC, verdict=Verdict.FAIL,
                     claims=[{"check": "AC-1", "expected": "x", "got": "y"}])
    s.finalize(aid, Resolution.ESCALATED)
    return path


def test_override_sets_resolution(db, capsys):
    assert main(["override", "1", "human_override", "--db", str(db)]) == 0
    assert AuditStore(db).get(1).resolution == Resolution.HUMAN_OVERRIDE
    assert "human_override" in capsys.readouterr().out


def test_override_accepts_reworked(db):
    assert main(["override", "1", "reworked", "--db", str(db)]) == 0
    assert AuditStore(db).get(1).resolution == Resolution.REWORKED


def test_override_rejects_unknown_resolution(db, capsys):
    with pytest.raises(SystemExit):
        main(["override", "1", "not_a_resolution", "--db", str(db)])


def test_override_unknown_attempt_returns_nonzero(db, capsys):
    assert main(["override", "999", "merged", "--db", str(db)]) == 1
    assert "没有" in capsys.readouterr().out


def test_defect_links_and_is_idempotent(db, capsys):
    assert main(["defect", "1", "BUG-42", "--db", str(db)]) == 0
    assert AuditStore(db).get(1).linked_defects == ["BUG-42"]
    # 同一个 defect 重复挂不该出现两次，否则漏报数会被重复计数
    assert main(["defect", "1", "BUG-42", "--db", str(db)]) == 0
    assert AuditStore(db).get(1).linked_defects == ["BUG-42"]
    assert main(["defect", "1", "BUG-43", "--db", str(db)]) == 0
    assert AuditStore(db).get(1).linked_defects == ["BUG-42", "BUG-43"]


def test_defect_unknown_attempt_returns_nonzero(db, capsys):
    assert main(["defect", "999", "BUG-1", "--db", str(db)]) == 1
    assert "没有" in capsys.readouterr().out


def test_metrics_command_prints_three_numbers(db, capsys):
    main(["override", "1", "reworked", "--db", str(db)])
    capsys.readouterr()
    assert main(["metrics", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "spec" in out
    assert "命中率" in out
    assert "漏报" in out
    assert "单位命中成本" in out


def test_metrics_on_empty_db_says_so(tmp_path, capsys):
    assert main(["metrics", "--db", str(tmp_path / "empty.db")]) == 0
    assert "没有" in capsys.readouterr().out


def test_metrics_command_reports_gate3(db, capsys):
    """P1 判据要能一眼看到，不然它就不是判据只是个字段。"""
    main(["override", "1", "reworked", "--db", str(db)])
    capsys.readouterr()

    assert main(["metrics", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "[闸门 3]" in out
    assert "上人平均打回次数: 1.00" in out
    assert "目标 ≤ 1" in out
    assert "达标" in out


def test_metrics_gate3_flags_a_failing_acceptance_system(db, capsys):
    """打回两次就超标，提示指向 checks 而不是 agent 能力。"""
    s = AuditStore(db)
    for _ in range(2):
        aid = s.open_attempt(
            task_id="T-bf", spec_ref=[], oracle_class=OracleClass.A,
            class_reason="A", harness="claude_code",
            harness_version="2.1", model="haiku",
        )
        s.finalize(aid, Resolution.REWORKED)
    main(["override", "1", "merged", "--db", str(db)])
    capsys.readouterr()

    assert main(["metrics", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "上人平均打回次数: 2.00" in out
    assert "未达标" in out
    assert "改 checks" in out


def test_metrics_gate3_shows_pending_when_nothing_dispatched(tmp_path, capsys):
    """空库也要有闸门 3 这一段，不能因为没数据就整块消失。"""
    assert main(["metrics", "--db", str(tmp_path / "e.db")]) == 0
    out = capsys.readouterr().out
    assert "没有裁决数据" in out
    assert "[闸门 3]" in out
    assert "P1 判据待测" in out
