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
    # 范围监工独立成一个角色，不并进 RISK：RISK 每轮已经写恰好一条裁决（后分级
    # PASS/FAIL）。共用角色会让同一个 attempt 出现两条方向相反的 RISK 裁决，
    # §5.1 的 fired 和 passed 会同时 +1，那张表的裁剪结论就建在坏数字上。
    SCOPE = "scope"
    # 金丝雀探针的可见性记录，不是一道闸门（§9.1）。探针 skipped 时原来什么都
    # 不写：审计里「验过是真的」和「根本没验」长得一模一样。非标测试目录
    # （spec/、t/）的仓库就落在这个盲区里 —— 恶意 agent 在那种仓库上伪造绿能
    # 直接出货，而报表上看不出任何异常。
    #
    # 独立成 role 而不是复用 RISK：BEACON 写的是「探针有没有真的跑」这个事实，
    # 每轮至多一条，且**从不 FAIL**（skipped 不该拦货，见 dispatcher 里的取舍）。
    # 混进 RISK 会让 §5.1 那张 fired/passed 表把「探针没跑」算成一次风险命中。
    BEACON = "beacon"


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
    # 人工定案时的理由（`factory resolve --why`）。resolution 只说了「最后怎么算」，
    # 说不出「为什么这么算」—— 两周后看 metrics 发现某个监工假阳性一堆，唯一能
    # 判断「是监工不行还是人图省事直接放行」的东西就是这一列。空串=没人工干预过。
    resolution_note: Mapped[str] = mapped_column(String(512), default="")
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


class PermissionEvent(Base):
    """权限门事件：一次非 ALLOW 的判决。

    只记 DENY 和 ESCALATE，ALLOW 不记（一次派发几百个 allow 全存下来审计库
    会被噪声灌满）。和 SupervisorVerdict 平级：都是一次 attempt 里的质量数据。
    """
    __tablename__ = "permission_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempt.id"))

    #: 工具名，如 "Write" / "Bash"
    tool: Mapped[str] = mapped_column(String(64))
    #: 目标（文件路径或命令），截断到 500 字符
    target: Mapped[str] = mapped_column(String(512))
    #: allow / deny / escalate
    decision: Mapped[str] = mapped_column(String(16))
    #: 命中的规则名，如 "protected-path:.github/workflows/**" / "git-reset-hard"
    rule: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(512))

    #: 走了模型审批的话记成本，静态规则判的是 0
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
