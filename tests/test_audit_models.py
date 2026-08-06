import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from factory.audit.models import (
    Base, TaskAttempt, SupervisorVerdict,
    OracleClass, Resolution, Verdict, SupervisorRole, utc_now,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _attempt(**kw):
    defaults = dict(
        task_id="T-1", attempt_no=1, spec_ref=["AC-1", "AC-2"],
        oracle_class=OracleClass.A, class_reason="default: no rule matched",
        harness="claude_code", harness_version="2.1.223", model="haiku",
        wall_clock_ms=1234,
    )
    return TaskAttempt(**{**defaults, **kw})


def test_utc_now_is_naive():
    assert utc_now().tzinfo is None


def test_roundtrip_json_and_enum_columns(session):
    session.add(_attempt())
    session.commit()

    row = session.query(TaskAttempt).one()
    assert row.spec_ref == ["AC-1", "AC-2"]
    assert row.oracle_class == OracleClass.A
    assert row.resolution == Resolution.PENDING       # 默认值
    assert row.linked_defects == []                   # 默认空 JSON 列表
    assert row.tokens_in == 0 and row.cost_usd == 0.0
    assert isinstance(row.created_at, datetime)
    assert row.created_at.tzinfo is None              # SQLite 不保留 tz


def test_unique_task_id_attempt_no(session):
    session.add(_attempt())
    session.commit()
    session.add(_attempt())
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_task_different_attempt_no_allowed(session):
    session.add(_attempt(attempt_no=1))
    session.add(_attempt(attempt_no=2))
    session.commit()
    assert session.query(TaskAttempt).count() == 2


def test_supervisor_verdicts_cascade(session):
    a = _attempt()
    a.supervisors.append(SupervisorVerdict(
        role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{"check": "pytest", "expected": "exit 0", "got": "exit 1"}],
    ))
    session.add(a)
    session.commit()

    row = session.query(TaskAttempt).one()
    assert len(row.supervisors) == 1
    assert row.supervisors[0].verdict == Verdict.FAIL
    assert row.supervisors[0].claims[0]["check"] == "pytest"

    session.delete(row)
    session.commit()
    assert session.query(SupervisorVerdict).count() == 0
