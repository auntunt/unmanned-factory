import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from factory.audit.models import (
    Base, HumanAction, HumanEvent, HumanGate, TaskAttempt, SupervisorVerdict,
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


# ---------- 人时账：resolved_at + human_event ----------

def test_resolved_at_defaults_to_none(session):
    """attempt 刚开时还没被人处理完 —— 必须是 None，不是「刚才」。

    人时账的闸门 3 用时 = override 时刻 − resolved_at。默认填 utc_now 的话，
    一个从没被判过的 attempt 会算出一个看起来正常的负数或零，而「没数据」
    和「零分钟」是两回事。
    """
    session.add(_attempt())
    session.commit()
    assert session.query(TaskAttempt).one().resolved_at is None


def test_resolved_at_is_naive_when_set(session):
    a = _attempt()
    a.resolved_at = utc_now()
    session.add(a)
    session.commit()
    row = session.query(TaskAttempt).one()
    assert isinstance(row.resolved_at, datetime)
    assert row.resolved_at.tzinfo is None


def test_human_event_roundtrip_with_naive_timestamp(session):
    session.add(HumanEvent(
        task_id="T-1", gate=HumanGate.INTAKE, action=HumanAction.BLOCKED,
    ))
    session.commit()
    row = session.query(HumanEvent).one()
    assert row.task_id == "T-1"
    assert row.gate == HumanGate.INTAKE
    assert row.action == HumanAction.BLOCKED
    assert row.note is None
    assert isinstance(row.created_at, datetime)
    assert row.created_at.tzinfo is None


def test_human_event_is_not_tied_to_an_attempt(session):
    """闸门 1 的两个事件都发生在 attempt 存在之前，所以只能挂 task_id。

    草稿被拦下时还没派发过，没有 attempt 行可挂外键；等人确认放回 inbox
    才会有 attempt。用 task_id 关联是这条链唯一贯通的键。
    """
    session.add(HumanEvent(
        task_id="T-未派发", gate=HumanGate.INTAKE, action=HumanAction.CONFIRM,
    ))
    session.commit()
    assert session.query(TaskAttempt).count() == 0
    assert session.query(HumanEvent).one().task_id == "T-未派发"
