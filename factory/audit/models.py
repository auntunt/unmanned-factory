"""审计数据模型。spec §5 的 task_attempt 字段清单在这里落地。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.types import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """全项目唯一时间来源。

    SQLite 上带 tzinfo 的值读回来 tzinfo 也是 None，所以这里直接存 naive UTC，
    不假装带时区，避免「写进去 aware、读出来 naive」的静默不一致。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class OracleClass(StrEnum):
    A = "A"   # 机器可判、四性质齐备 → 全自动
    B = "B"   # 可判但影响面大 → 自动执行 + 对抗 review
    C = "C"   # 无廉价裁判 → 不无人
    D = "D"   # 不可逆 → 硬闸门，只生成脚本


class Resolution(StrEnum):
    PENDING = "pending"
    MERGED = "merged"
    REWORKED = "reworked"
    HUMAN_OVERRIDE = "human_override"
    ESCALATED = "escalated"


# 预分级就拦下的 attempt 从没调过 harness。用它区分「派发过」和「没派发过」——
# P1 判据「闸门 3 上人平均打回次数」的分母必须排除这些，否则拦得越多指标越好看。
NOT_DISPATCHED = "n/a"


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class SupervisorRole(StrEnum):
    REGRESSION = "regression"
    SPEC = "spec"
    ARCHITECTURE = "architecture"
    RISK = "risk"


class Base(DeclarativeBase):
    pass


class TaskAttempt(Base):
    __tablename__ = "task_attempt"
    __table_args__ = (UniqueConstraint("task_id", "attempt_no", name="uq_task_attempt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    task_id: Mapped[str] = mapped_column(String(128))
    attempt_no: Mapped[int] = mapped_column(Integer)
    spec_ref: Mapped[list[str]] = mapped_column(JSON, default=list)

    oracle_class: Mapped[OracleClass] = mapped_column(String(1))
    class_reason: Mapped[str] = mapped_column(String(512))

    harness: Mapped[str] = mapped_column(String(64))
    harness_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))

    diff_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    commit: Mapped[str | None] = mapped_column(String(64), default=None)
    transcript_path: Mapped[str | None] = mapped_column(String(1024), default=None)

    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    wall_clock_ms: Mapped[int] = mapped_column(Integer, default=0)

    resolution: Mapped[Resolution] = mapped_column(String(16), default=Resolution.PENDING)
    linked_defects: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    supervisors: Mapped[list["SupervisorVerdict"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan",
    )


class SupervisorVerdict(Base):
    __tablename__ = "supervisor_verdict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempt.id"))

    role: Mapped[SupervisorRole] = mapped_column(String(16))
    verdict: Mapped[Verdict] = mapped_column(String(8))
    claims: Mapped[list[dict]] = mapped_column(JSON, default=list)

    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    attempt: Mapped[TaskAttempt] = relationship(back_populates="supervisors")
