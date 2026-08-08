# P0 骨架实现计划：无人工厂编排层

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task with TDD discipline.

## Goal

实现 spec §9 的 P0：**adapter（先接 1 个 harness）+ 回归监工 + 审计存储 + 静态分级规则**。

完成判据（spec 原文）：**一个 A 类任务端到端跑通，审计记录字段完整**。

具体到可验证的行为：给定一个 A 类任务（有廉价客观裁判、幂等、可逆、失败知识已编码），
系统能自动派发给 Claude Code、捕获 diff 与 transcript、跑回归监工、按结果决定
merge / 打回重试（最多 3 轮）/ 升级给人，并把 `task_attempt` 的每个字段落库。

## Architecture

```
                 ┌──────────────┐
   task.yaml ──▶ │ 分级规则引擎  │──(1) 预分级 declared_paths
                 └──────┬───────┘
                        │ class A/B → 继续；C/D → 直接升级给人
                        ▼
                 ┌──────────────┐      ┌─────────────────┐
                 │  dispatcher   │─────▶│ ClaudeCodeAdapter│──▶ claude -p
                 └──────┬───────┘      └────────┬────────┘
                        │                       │ AttemptResult
                        │◀──────────────────────┘ (diff/transcript/tokens)
                        │
                        ├─(2) 后分级 实际改动文件 → 比预分级更严则升级
                        │
                        ├──▶ 回归监工（确定性，无模型调用）
                        │        跑命令、比对输出 → verdict + claims
                        │
                        └──▶ 审计存储（SQLite）task_attempt + supervisor_verdict
```

分级规则引擎跑**两次**是核心设计：第一次在派发前用任务声明的 `declared_paths`，
第二次在拿到 diff 后用**真实改动的文件**。第二次比第一次严重就升级。
所以 spec §4 里的「风险监工」不是独立组件，就是这台引擎的第二次调用。

回归监工与风险监工都是**确定性代码，不调模型**。因此 P0 的监工模型成本为 0，
且 P0 的全部单测可以离线跑（只有一个端到端 smoke test 需要真的调 `claude`）。

## Tech Stack

- Python 3.12.7（`StrEnum` 需要 3.11+）
- SQLAlchemy 2.0.50（`DeclarativeBase` / `Mapped` / `mapped_column`）
- SQLite（`create_all`，P0 不做 migration）
- PyYAML 6.0.2（任务定义、分级规则、路由表）
- pytest 9.0.3
- uv 0.5.15 管依赖
- harness：Claude Code CLI（`claude -p --output-format json`）

## Global Constraints

**已验证的事实，实现时必须照此写，不要「优化」掉：**

1. `claude -p` 的**退出码永远是 0**，即使 `--max-turns` 打满。成功/失败只能读 JSON 里的
   `is_error` 字段。不要用 `returncode` 判断。
2. transcript 路径是 `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`，slug 是有损的
   （`_`→`-`，中文目录整段变成 `-`）。**必须用 `glob("*/<session-id>.jsonl")` 定位，
   不要试图从 cwd 重算 slug。**
3. diff 捕获用 `git add -A -N` + `git diff HEAD`。这样能拿到修改、新增、以及嵌套新目录里的
   新文件，且 `-N` 只登记 intent-to-add、不 stage 内容，无副作用。前提是 workspace 至少有
   1 个 commit（先 `git rev-parse --verify HEAD`）。
4. SQLite 上 `DateTime` 和 `DateTime(timezone=True)` 读回来 `tzinfo` **都是 `None`**。
   统一存 naive UTC（`datetime.now(UTC).replace(tzinfo=None)`），不要假装带时区。
5. `fnmatch` 的 `*` **跨 `/`**：`fnmatch("src/app/migrations/001.py", "*migrations/*")` 为 True。
   分级规则直接用 fnmatch，不要引 regex。
6. `--max-turns` 在 CLI 2.1.223 里**能用但 `--help` 里没有**。用它，但在 adapter 里注释标注
   这是未文档化的依赖，且 `limits` 里允许不传。
7. `StrEnum` 值落库是普通字符串，读回来 `== OracleClass.A` 为 True。
8. `UniqueConstraint("task_id","attempt_no")` 在 SQLite 上真的抛 `IntegrityError`。重试计数
   靠它兜底，不要在应用层再手写一遍查重。

**安全边界（不可协商）：**

9. C 类（认证/鉴权/加密/密钥/支付/事务边界/新 UX）**永不无人**。
10. D 类（schema 迁移/数据删除/生产部署/force-push）是**硬闸门、非旁路**。agent 只能生成
    待执行脚本，**绝不自己执行**。dispatcher 里不允许存在任何绕过 D 类判定的代码路径。
11. 任何配置或日志里出现的密码、API key，只按 key 名引用，**不落库、不打印明文值**。

## File Structure

```
pyproject.toml
factory/
  __init__.py
  audit/
    __init__.py
    models.py          # Task 1: ORM — TaskAttempt / SupervisorVerdict + 枚举
    store.py           # Task 2: AuditStore — open/record/finalize
  grading/
    __init__.py
    rules.py           # Task 3: 分级规则引擎（fnmatch，跑两次）
    oracle_rules.yaml  # Task 3: 静态规则库
  harness/
    __init__.py
    base.py            # Task 4: AttemptResult + HarnessAdapter Protocol
    workspace.py       # Task 4: git diff 捕获
    claude_code.py     # Task 5: ClaudeCodeAdapter
  supervisors/
    __init__.py
    regression.py      # Task 6: 回归监工（确定性）
  dispatcher.py        # Task 7: 编排循环 + 三轮重试 + D 类硬闸门
  routing.yaml         # Task 7: 模型分级路由表
  cli.py               # Task 8: 入口
tests/
  test_audit_models.py       # Task 1
  test_audit_store.py        # Task 2
  test_grading.py            # Task 3
  test_workspace.py          # Task 4
  test_claude_code.py        # Task 5
  test_regression.py         # Task 6
  test_dispatcher.py         # Task 7
  test_e2e_smoke.py          # Task 8（唯一真调 claude 的测试，标 @pytest.mark.smoke）
```

---

### Task 1: 审计数据模型

**Files:**
- Create: `pyproject.toml`
- Create: `factory/__init__.py`, `factory/audit/__init__.py`
- Create: `factory/audit/models.py`
- Test: `tests/test_audit_models.py`

**Interfaces:**

Produces:
```python
class OracleClass(StrEnum):   A = "A"; B = "B"; C = "C"; D = "D"
class Resolution(StrEnum):    PENDING = "pending"; MERGED = "merged"
                              REWORKED = "reworked"; HUMAN_OVERRIDE = "human_override"
                              ESCALATED = "escalated"
class Verdict(StrEnum):       PASS = "pass"; FAIL = "fail"
class SupervisorRole(StrEnum): REGRESSION = "regression"; SPEC = "spec"
                               ARCHITECTURE = "architecture"; RISK = "risk"

class Base(DeclarativeBase): ...

class TaskAttempt(Base):
    id: Mapped[int]                       # pk
    task_id: Mapped[str]
    attempt_no: Mapped[int]
    spec_ref: Mapped[list[str]]           # JSON
    oracle_class: Mapped[OracleClass]
    class_reason: Mapped[str]
    harness: Mapped[str]
    harness_version: Mapped[str]
    model: Mapped[str]
    diff_hash: Mapped[str | None]
    commit: Mapped[str | None]
    transcript_path: Mapped[str | None]
    tokens_in / tokens_out: Mapped[int]
    cost_usd: Mapped[float]
    wall_clock_ms: Mapped[int]
    resolution: Mapped[Resolution]
    linked_defects: Mapped[list[str]]     # JSON，事后回填
    created_at: Mapped[datetime]          # naive UTC
    supervisors: Mapped[list["SupervisorVerdict"]]
    __table_args__ = (UniqueConstraint("task_id", "attempt_no", name="uq_task_attempt"),)

class SupervisorVerdict(Base):
    id: Mapped[int]                       # pk
    attempt_id: Mapped[int]               # fk -> task_attempt.id
    role: Mapped[SupervisorRole]
    verdict: Mapped[Verdict]
    claims: Mapped[list[dict]]            # JSON
    tokens: Mapped[int]
    cost_usd: Mapped[float]
    attempt: Mapped[TaskAttempt]

def utc_now() -> datetime               # naive UTC，全项目唯一时间来源
```

**Steps:**

- [x] 写 `pyproject.toml`

```toml
[project]
name = "unmanned-factory"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "SQLAlchemy==2.0.50",
    "PyYAML==6.0.2",
]

[dependency-groups]
dev = ["pytest==9.0.3"]

[tool.pytest.ini_options]
pythonpath = ["."]
markers = ["smoke: 需要真实调用 claude CLI 的端到端测试"]
```

- [x] 写失败测试 `tests/test_audit_models.py`

```python
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
```

- [x] 运行 `uv run pytest tests/test_audit_models.py` — 确认因 `ModuleNotFoundError: factory` 失败

- [x] 实现 `factory/audit/models.py`（`factory/__init__.py` 与 `factory/audit/__init__.py` 留空）

```python
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
```

- [x] 运行 `uv run pytest tests/test_audit_models.py` — 5 个测试全过
- [x] 提交：`git add -A && git commit -m "feat(audit): task_attempt / supervisor_verdict ORM 模型"`

---

### Task 2: 审计存储 API

**Files:**
- Create: `factory/audit/store.py`
- Test: `tests/test_audit_store.py`

**Interfaces:**

Consumes: `factory.audit.models`（Task 1 全部导出）

Produces:
```python
class AuditStore:
    def __init__(self, db_path: str | Path) -> None      # ":memory:" 或文件路径
    def next_attempt_no(self, task_id: str) -> int       # 无记录时返回 1
    def open_attempt(
        self, *, task_id: str, spec_ref: list[str],
        oracle_class: OracleClass, class_reason: str,
        harness: str, harness_version: str, model: str,
    ) -> int                                             # 返回 attempt_id，attempt_no 自动递增
    def record_result(
        self, attempt_id: int, *,
        diff_hash: str | None, commit: str | None, transcript_path: str | None,
        tokens_in: int, tokens_out: int, cost_usd: float, wall_clock_ms: int,
        harness_version: str | None = None,   # 非 None 时覆盖写
    ) -> None
    def record_verdict(
        self, attempt_id: int, *,
        role: SupervisorRole, verdict: Verdict, claims: list[dict],
        tokens: int = 0, cost_usd: float = 0.0,
    ) -> None
    def escalate_class(self, attempt_id: int, *,
                       oracle_class: OracleClass, class_reason: str) -> None
    def finalize(self, attempt_id: int, resolution: Resolution) -> None
    def link_defect(self, attempt_id: int, defect_id: str) -> None
    def get(self, attempt_id: int) -> TaskAttempt        # 含 supervisors，session 已 expunge
```

**Steps:**

- [x] 写失败测试 `tests/test_audit_store.py`

```python
import pytest

from factory.audit.models import OracleClass, Resolution, SupervisorRole, Verdict
from factory.audit.store import AuditStore


@pytest.fixture
def store():
    return AuditStore(":memory:")


def _open(store, task_id="T-1", oracle_class=OracleClass.A):
    return store.open_attempt(
        task_id=task_id, spec_ref=["AC-1"],
        oracle_class=oracle_class, class_reason="no rule matched -> default A",
        harness="claude_code", harness_version="2.1.223", model="haiku",
    )


def test_next_attempt_no_starts_at_one(store):
    assert store.next_attempt_no("T-1") == 1


def test_attempt_no_increments_per_task(store):
    _open(store, "T-1")
    _open(store, "T-1")
    _open(store, "T-2")
    assert store.next_attempt_no("T-1") == 3
    assert store.next_attempt_no("T-2") == 2


def test_open_attempt_persists_all_fields(store):
    aid = _open(store)
    row = store.get(aid)
    assert row.task_id == "T-1"
    assert row.attempt_no == 1
    assert row.spec_ref == ["AC-1"]
    assert row.oracle_class == OracleClass.A
    assert row.harness == "claude_code"
    assert row.model == "haiku"
    assert row.resolution == Resolution.PENDING


def test_record_result_then_verdict_then_finalize(store):
    aid = _open(store)
    store.record_result(
        aid, diff_hash="abc123", commit="deadbeef",
        transcript_path="/tmp/x.jsonl",
        tokens_in=100, tokens_out=50, cost_usd=0.0021, wall_clock_ms=4200,
    )
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.PASS, claims=[],
    )
    store.finalize(aid, Resolution.MERGED)

    row = store.get(aid)
    assert row.diff_hash == "abc123"
    assert row.commit == "deadbeef"
    assert row.transcript_path == "/tmp/x.jsonl"
    assert (row.tokens_in, row.tokens_out) == (100, 50)
    assert row.cost_usd == pytest.approx(0.0021)
    assert row.wall_clock_ms == 4200
    assert row.resolution == Resolution.MERGED
    assert len(row.supervisors) == 1
    assert row.supervisors[0].role == SupervisorRole.REGRESSION


def test_multiple_verdicts_accumulate(store):
    aid = _open(store)
    store.record_verdict(aid, role=SupervisorRole.REGRESSION,
                         verdict=Verdict.FAIL, claims=[{"check": "pytest"}])
    store.record_verdict(aid, role=SupervisorRole.RISK,
                         verdict=Verdict.PASS, claims=[])
    roles = {v.role for v in store.get(aid).supervisors}
    assert roles == {SupervisorRole.REGRESSION, SupervisorRole.RISK}


def test_escalate_class_overwrites_class_and_reason(store):
    aid = _open(store, oracle_class=OracleClass.A)
    store.escalate_class(aid, oracle_class=OracleClass.D,
                         class_reason="post-diff: touched migrations/001.py")
    row = store.get(aid)
    assert row.oracle_class == OracleClass.D
    assert "migrations/001.py" in row.class_reason


def test_link_defect_appends(store):
    aid = _open(store)
    store.link_defect(aid, "BUG-1")
    store.link_defect(aid, "BUG-2")
    assert store.get(aid).linked_defects == ["BUG-1", "BUG-2"]


def test_persists_across_store_instances(tmp_path):
    db = tmp_path / "audit.db"
    aid = _open(AuditStore(db))
    reopened = AuditStore(db)
    assert reopened.get(aid).task_id == "T-1"
    assert reopened.next_attempt_no("T-1") == 2
```

- [x] 运行 `uv run pytest tests/test_audit_store.py` — 确认因 `factory.audit.store` 不存在失败

- [x] 实现 `factory/audit/store.py`

```python
"""审计存储。task_attempt 是原子审计单元，一次 attempt 一行。

resolution 和 linked_defects 是承重字段 —— 两周后算监工命中率
（监工报 FAIL 的 attempt 里有多少真的 reworked；监工报 PASS 的里有多少事后
挂上了 linked_defects）全靠这两个字段，所以宁可留空也不要写错。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, selectinload

from factory.audit.models import (
    Base, OracleClass, Resolution, SupervisorRole, SupervisorVerdict,
    TaskAttempt, Verdict,
)


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
        # in-memory 时用 StaticPool 之外的连接会各自拿到独立空库，
        # 但这里每个 AuditStore 自己持有 engine，同一实例内的 Session 共享连接池即可。
        self._engine = create_engine(url)
        Base.metadata.create_all(self._engine)

    def _session(self) -> Session:
        return Session(self._engine)

    def next_attempt_no(self, task_id: str) -> int:
        with self._session() as s:
            current = s.scalar(
                select(func.max(TaskAttempt.attempt_no)).where(
                    TaskAttempt.task_id == task_id
                )
            )
            return (current or 0) + 1

    def open_attempt(
        self,
        *,
        task_id: str,
        spec_ref: list[str],
        oracle_class: OracleClass,
        class_reason: str,
        harness: str,
        harness_version: str,
        model: str,
    ) -> int:
        with self._session() as s:
            current = s.scalar(
                select(func.max(TaskAttempt.attempt_no)).where(
                    TaskAttempt.task_id == task_id
                )
            )
            row = TaskAttempt(
                task_id=task_id,
                attempt_no=(current or 0) + 1,
                spec_ref=spec_ref,
                oracle_class=oracle_class,
                class_reason=class_reason,
                harness=harness,
                harness_version=harness_version,
                model=model,
            )
            s.add(row)
            s.commit()
            return row.id

    def record_result(
        self,
        attempt_id: int,
        *,
        diff_hash: str | None,
        commit: str | None,
        transcript_path: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        wall_clock_ms: int,
    ) -> None:
        with self._session() as s:
            row = s.get(TaskAttempt, attempt_id)
            row.diff_hash = diff_hash
            row.commit = commit
            row.transcript_path = transcript_path
            row.tokens_in = tokens_in
            row.tokens_out = tokens_out
            row.cost_usd = cost_usd
            row.wall_clock_ms = wall_clock_ms
            s.commit()

    def record_verdict(
        self,
        attempt_id: int,
        *,
        role: SupervisorRole,
        verdict: Verdict,
        claims: list[dict],
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._session() as s:
            s.add(SupervisorVerdict(
                attempt_id=attempt_id, role=role, verdict=verdict,
                claims=claims, tokens=tokens, cost_usd=cost_usd,
            ))
            s.commit()

    def escalate_class(
        self, attempt_id: int, *, oracle_class: OracleClass, class_reason: str
    ) -> None:
        """后分级比预分级更严时改写。class_reason 覆盖写，保留判定依据可回溯。"""
        with self._session() as s:
            row = s.get(TaskAttempt, attempt_id)
            row.oracle_class = oracle_class
            row.class_reason = class_reason
            s.commit()

    def finalize(self, attempt_id: int, resolution: Resolution) -> None:
        with self._session() as s:
            s.get(TaskAttempt, attempt_id).resolution = resolution
            s.commit()

    def link_defect(self, attempt_id: int, defect_id: str) -> None:
        with self._session() as s:
            row = s.get(TaskAttempt, attempt_id)
            # JSON 列必须整体重新赋值，原地 append 不会被 ORM 检测为脏
            row.linked_defects = [*row.linked_defects, defect_id]
            s.commit()

    def get(self, attempt_id: int) -> TaskAttempt:
        with self._session() as s:
            row = s.scalar(
                select(TaskAttempt)
                .options(selectinload(TaskAttempt.supervisors))
                .where(TaskAttempt.id == attempt_id)
            )
            s.expunge_all()
            return row
```

- [x] 运行 `uv run pytest tests/test_audit_store.py` — 8 个测试全过
- [x] 提交：`git add -A && git commit -m "feat(audit): AuditStore 读写 API"`

---

### Task 3: 静态分级规则引擎

**Files:**
- Create: `factory/grading/__init__.py`, `factory/grading/rules.py`, `factory/grading/oracle_rules.yaml`
- Test: `tests/test_grading.py`

**Interfaces:**

Consumes: `factory.audit.models.OracleClass`

Produces:
```python
SEVERITY: dict[OracleClass, int]          # A=0 B=1 C=2 D=3
DEFAULT_REASON: str                       # "no rule matched -> default A"

@dataclass(frozen=True)
class Rule:
    oracle_class: OracleClass
    reason: str
    patterns: tuple[str, ...] = ()        # fnmatch，匹配文件路径
    ops: tuple[str, ...] = ()             # 精确匹配声明的操作名
    def match(self, paths: list[str], ops: list[str]) -> tuple[str, ...]

@dataclass(frozen=True)
class Grade:
    oracle_class: OracleClass
    reason: str
    triggers: tuple[str, ...] = ()
    @property unmanned_allowed: bool      # A/B 为 True
    @property hard_gate: bool             # D 为 True
    def more_severe_than(self, other: "Grade") -> bool

class GradingEngine:
    def __init__(self, rules: Sequence[Rule]) -> None
    @classmethod def from_yaml(cls, path: str | Path) -> "GradingEngine"
    @classmethod def default(cls) -> "GradingEngine"     # 读内置 oracle_rules.yaml
    def grade(self, paths: Iterable[str] = (), ops: Iterable[str] = ()) -> Grade
```

**Steps:**

- [x] 写失败测试 `tests/test_grading.py`

```python
import pytest

from factory.audit.models import OracleClass
from factory.grading.rules import DEFAULT_REASON, Grade, GradingEngine, Rule


def test_no_match_defaults_to_a():
    g = GradingEngine([]).grade(["README.md"])
    assert g.oracle_class == OracleClass.A
    assert g.reason == DEFAULT_REASON
    assert g.unmanned_allowed is True
    assert g.hard_gate is False


def test_fnmatch_star_crosses_slashes():
    engine = GradingEngine([
        Rule(OracleClass.D, "schema migration", patterns=("*migrations/*",)),
    ])
    assert engine.grade(["src/app/migrations/001.py"]).oracle_class == OracleClass.D
    assert engine.grade(["migrations/001.py"]).oracle_class == OracleClass.D
    assert engine.grade(["src/models.py"]).oracle_class == OracleClass.A


def test_most_severe_rule_wins_regardless_of_order():
    engine = GradingEngine([
        Rule(OracleClass.D, "migration", patterns=("*migrations/*",)),
        Rule(OracleClass.B, "api surface", patterns=("*api/*",)),
    ])
    g = engine.grade(["src/api/v1.py", "db/migrations/003.py"])
    assert g.oracle_class == OracleClass.D
    assert "migration" in g.reason


def test_reason_carries_triggering_paths_for_traceability():
    engine = GradingEngine([Rule(OracleClass.C, "auth", patterns=("*auth/*",))])
    g = engine.grade(["src/auth/login.py"])
    assert g.triggers == ("src/auth/login.py",)
    assert "src/auth/login.py" in g.reason


def test_ops_match_without_any_file_change():
    """生产部署改动 0 个文件，也必须判成 D。"""
    engine = GradingEngine([
        Rule(OracleClass.D, "irreversible op", ops=("prod_deploy",)),
    ])
    g = engine.grade(paths=[], ops=["prod_deploy"])
    assert g.oracle_class == OracleClass.D
    assert g.hard_gate is True


def test_grade_comparison():
    a = Grade(OracleClass.A, "x")
    d = Grade(OracleClass.D, "y")
    assert d.more_severe_than(a) is True
    assert a.more_severe_than(d) is False
    assert a.more_severe_than(Grade(OracleClass.A, "z")) is False


def test_from_yaml(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(
        "rules:\n"
        "  - class: D\n"
        "    reason: data deletion\n"
        "    ops: [data_delete]\n"
        "  - class: C\n"
        "    reason: crypto\n"
        "    patterns: ['*crypto*']\n",
        encoding="utf-8",
    )
    engine = GradingEngine.from_yaml(p)
    assert engine.grade(ops=["data_delete"]).oracle_class == OracleClass.D
    assert engine.grade(["lib/crypto_utils.py"]).oracle_class == OracleClass.C


@pytest.mark.parametrize(
    "path,expected",
    [
        ("db/migrations/0007_add_col.py", OracleClass.D),
        ("infra/main.tf", OracleClass.D),
        ("src/auth/session.py", OracleClass.C),
        ("app/.env.production", OracleClass.C),
        ("src/payment/charge.py", OracleClass.C),
        ("src/api/users.py", OracleClass.B),
        ("src/models.py", OracleClass.B),
        ("docs/readme.md", OracleClass.A),
        ("tests/test_utils.py", OracleClass.A),
    ],
)
def test_builtin_ruleset(path, expected):
    assert GradingEngine.default().grade([path]).oracle_class == expected


@pytest.mark.parametrize("op", ["prod_deploy", "data_delete", "force_push",
                                "schema_migration"])
def test_builtin_ruleset_ops_are_all_hard_gate(op):
    assert GradingEngine.default().grade(ops=[op]).oracle_class == OracleClass.D
```

- [x] 运行 `uv run pytest tests/test_grading.py` — 确认因模块不存在失败

- [x] 实现 `factory/grading/oracle_rules.yaml`

```yaml
# 静态分级规则库。spec §3.3。
# 规则顺序不影响结果：最严的命中规则胜出（D > C > B > A）。
# 不命中任何规则 → A（全自动）。这是「默认放行」，所以下面的 C/D 段
# 宁可写宽也不要写窄。
rules:
  # ---- D：不可逆。硬闸门、非旁路，agent 只能生成待执行脚本 ----
  - class: D
    reason: "D: schema migration"
    patterns: ["*migrations/*", "*migration/*", "*alembic/*", "*.sql"]
  - class: D
    reason: "D: infrastructure as code"
    patterns: ["*.tf", "*.tfvars", "*.tfstate", "*k8s/*", "*helm/*"]
  - class: D
    reason: "D: deploy script"
    patterns: ["*deploy*.sh", "*deploy*.yml", "*deploy*.yaml",
               "*.github/workflows/*"]
  - class: D
    reason: "D: irreversible operation declared"
    ops: ["prod_deploy", "data_delete", "schema_migration", "force_push",
          "drop_table", "truncate", "registry_push"]

  # ---- C：无廉价裁判。永不无人 ----
  - class: C
    reason: "C: auth / authz"
    patterns: ["*auth/*", "*auth.py", "*login*", "*permission*", "*rbac*",
               "*session/*"]
  - class: C
    reason: "C: crypto / secrets"
    patterns: ["*crypto*", "*secret*", "*.env", "*.env.*", "*credential*",
               "*keystore*", "*token*"]
  - class: C
    reason: "C: payment / transaction boundary"
    patterns: ["*payment*", "*billing*", "*invoice*", "*transaction*",
               "*refund*"]
  - class: C
    reason: "C: new UX surface"
    ops: ["new_ux", "visual_change"]

  # ---- B：可判但影响面大。自动执行 + 对抗 review（P1 才有对抗 review）----
  - class: B
    reason: "B: public API surface"
    patterns: ["*api/*", "*routes/*", "*endpoints/*", "*schema.py",
               "*serializers*"]
  - class: B
    reason: "B: data model"
    patterns: ["*models.py", "*models/*", "*entities/*"]
  - class: B
    reason: "B: shared config / core"
    patterns: ["*settings*", "*config*", "*core/*", "pyproject.toml",
               "package.json", "*Dockerfile*", "*docker-compose*"]
```

- [x] 实现 `factory/grading/rules.py`（`factory/grading/__init__.py` 留空）

```python
"""静态分级规则引擎。spec §3 按裁判成本分级。

这台引擎在一次 attempt 里跑两次：
  1. 派发前，用任务声明的 declared_paths / declared_ops → 决定要不要无人
  2. 拿到 diff 后，用真实改动的文件 → 比第一次严重就升级

spec §4 里的「风险监工」就是第二次调用，不是独立组件。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml

from factory.audit.models import OracleClass

SEVERITY: dict[OracleClass, int] = {
    OracleClass.A: 0,
    OracleClass.B: 1,
    OracleClass.C: 2,
    OracleClass.D: 3,
}

DEFAULT_REASON = "no rule matched -> default A"


@dataclass(frozen=True)
class Rule:
    oracle_class: OracleClass
    reason: str
    patterns: tuple[str, ...] = ()
    ops: tuple[str, ...] = ()

    def match(self, paths: list[str], ops: list[str]) -> tuple[str, ...]:
        hits: list[str] = [
            p for p in paths if any(fnmatch(p, pat) for pat in self.patterns)
        ]
        hits += [o for o in ops if o in self.ops]
        return tuple(dict.fromkeys(hits))  # 去重且保序


@dataclass(frozen=True)
class Grade:
    oracle_class: OracleClass
    reason: str
    triggers: tuple[str, ...] = ()

    @property
    def unmanned_allowed(self) -> bool:
        """只有 A/B 允许无人。C 永不无人，D 是硬闸门。"""
        return self.oracle_class in (OracleClass.A, OracleClass.B)

    @property
    def hard_gate(self) -> bool:
        return self.oracle_class == OracleClass.D

    def more_severe_than(self, other: Grade) -> bool:
        return SEVERITY[self.oracle_class] > SEVERITY[other.oracle_class]


class GradingEngine:
    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def from_yaml(cls, path: str | Path) -> GradingEngine:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls([
            Rule(
                oracle_class=OracleClass(r["class"]),
                reason=r["reason"],
                patterns=tuple(r.get("patterns", ())),
                ops=tuple(r.get("ops", ())),
            )
            for r in doc.get("rules", [])
        ])

    @classmethod
    def default(cls) -> GradingEngine:
        return cls.from_yaml(Path(__file__).with_name("oracle_rules.yaml"))

    def grade(
        self, paths: Iterable[str] = (), ops: Iterable[str] = ()
    ) -> Grade:
        path_list, op_list = list(paths), list(ops)
        worst: Grade | None = None
        for rule in self._rules:
            hits = rule.match(path_list, op_list)
            if not hits:
                continue
            candidate = Grade(
                oracle_class=rule.oracle_class,
                reason=f"{rule.reason} [{', '.join(hits)}]",
                triggers=hits,
            )
            if worst is None or candidate.more_severe_than(worst):
                worst = candidate
        return worst or Grade(OracleClass.A, DEFAULT_REASON, ())
```

- [x] 运行 `uv run pytest tests/test_grading.py` — 全过（含 13 个参数化用例）
- [x] 提交：`git add -A && git commit -m "feat(grading): 静态分级规则引擎 + 内置规则库"`

---

### Task 4: 任务定义 + workspace diff 捕获 + harness 契约

**Files:**
- Create: `factory/task.py`
- Create: `factory/harness/__init__.py`, `factory/harness/base.py`, `factory/harness/workspace.py`
- Test: `tests/test_task.py`, `tests/test_workspace.py`

**Interfaces:**

Produces:
```python
# factory/task.py
@dataclass(frozen=True)
class CheckSpec:
    name: str
    command: str
    expect: str = "exit_zero"     # exit_zero | stdout_contains | commands_agree
    value: str = ""               # stdout_contains 的子串 / commands_agree 的对照命令
    timeout_s: int = 300

@dataclass(frozen=True)
class Task:
    task_id: str
    prompt: str
    spec_ref: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    declared_ops: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    max_rounds: int = 3
    @classmethod def from_yaml(cls, path: str | Path) -> "Task"

# factory/harness/base.py
class ExitStatus(StrEnum): OK="ok"; ERROR="error"; TIMEOUT="timeout"

@dataclass(frozen=True)
class ToolCall: name: str; call_id: str; is_error: bool = False

@dataclass(frozen=True)
class Limits: max_turns: int | None = None; timeout_s: int = 900

@dataclass(frozen=True)
class AttemptResult:
    exit_status: ExitStatus
    diff: str = ""
    diff_hash: str | None = None
    changed_paths: tuple[str, ...] = ()
    transcript_path: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_clock_ms: int = 0
    session_id: str | None = None
    harness_version: str = "unknown"
    error_text: str = ""

class HarnessAdapter(Protocol):
    name: str
    def run(self, task: Task, workspace: Path, limits: Limits,
            *, model: str | None = None) -> AttemptResult: ...

# factory/harness/workspace.py
def has_baseline(root: Path) -> bool                    # git rev-parse --verify HEAD
def capture_diff(root: Path) -> tuple[str, tuple[str, ...]]
def diff_hash(diff: str) -> str | None                  # 空 diff 返回 None
```

**契约说明（与 spec §7.1 的差异，有意为之）：** spec 写的是
`run(task, workspace, limits) -> AttemptResult`。这里位置参数完全保留，只多加一个
keyword-only 的 `model`，因为模型分级是路由第一轴（用户明确要求 opus/sonnet/haiku
可选），而它不属于「limits」。默认 `None` = 用 harness 自己的默认模型。

**Steps:**

- [x] 写失败测试 `tests/test_task.py`

```python
from factory.task import CheckSpec, Task


def test_from_yaml_full(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        "task_id: T-100\n"
        "prompt: |\n"
        "  在 greet.py 里加一个 greet(name) 函数\n"
        "spec_ref: [AC-1, AC-2]\n"
        "declared_paths: ['greet.py']\n"
        "declared_ops: []\n"
        "max_rounds: 2\n"
        "checks:\n"
        "  - name: pytest\n"
        "    command: python -m pytest -q\n"
        "  - name: image-id-agrees\n"
        "    command: echo abc\n"
        "    expect: commands_agree\n"
        "    value: echo abc\n"
        "  - name: has-greet\n"
        "    command: cat greet.py\n"
        "    expect: stdout_contains\n"
        "    value: 'def greet'\n",
        encoding="utf-8",
    )
    t = Task.from_yaml(p)
    assert t.task_id == "T-100"
    assert "greet(name)" in t.prompt
    assert t.spec_ref == ("AC-1", "AC-2")
    assert t.declared_paths == ("greet.py",)
    assert t.declared_ops == ()
    assert t.max_rounds == 2
    assert len(t.checks) == 3
    assert t.checks[0] == CheckSpec(name="pytest", command="python -m pytest -q")
    assert t.checks[1].expect == "commands_agree"
    assert t.checks[2].value == "def greet"


def test_from_yaml_minimal_defaults(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("task_id: T-1\nprompt: do it\n", encoding="utf-8")
    t = Task.from_yaml(p)
    assert t.checks == ()
    assert t.declared_paths == ()
    assert t.max_rounds == 3
```

- [x] 写失败测试 `tests/test_workspace.py`

```python
import subprocess

import pytest

from factory.harness.workspace import capture_diff, diff_hash, has_baseline


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "kept.txt").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def test_has_baseline(tmp_path, repo):
    assert has_baseline(repo) is True
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert has_baseline(empty) is False


def test_empty_diff(repo):
    diff, paths = capture_diff(repo)
    assert diff == ""
    assert paths == ()
    assert diff_hash(diff) is None


def test_captures_modified_added_and_nested(repo):
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    (repo / "added.txt").write_text("new\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")

    diff, paths = capture_diff(repo)
    assert set(paths) == {"kept.txt", "added.txt", "sub/deep.txt"}
    assert "changed" in diff and "deep" in diff


def test_captures_deletion(repo):
    (repo / "kept.txt").unlink()
    diff, paths = capture_diff(repo)
    assert paths == ("kept.txt",)
    assert "deleted file" in diff or "-original" in diff


def test_intent_to_add_does_not_stage_content(repo):
    (repo / "added.txt").write_text("new\n", encoding="utf-8")
    capture_diff(repo)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--stat"], cwd=repo,
        capture_output=True, text=True,
    ).stdout
    # -N 只登记 intent-to-add，内容没有被 stage
    assert "new" not in staged


def test_diff_hash_is_stable_and_content_sensitive(repo):
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    d1, _ = capture_diff(repo)
    assert diff_hash(d1) == diff_hash(d1)
    (repo / "kept.txt").write_text("changed twice\n", encoding="utf-8")
    d2, _ = capture_diff(repo)
    assert diff_hash(d1) != diff_hash(d2)
```

- [x] 运行 `uv run pytest tests/test_task.py tests/test_workspace.py` — 确认失败

- [x] 实现 `factory/task.py`

```python
"""任务定义。P0 从 YAML 读，P1 才由 PRD 自动生成。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CheckSpec:
    """回归监工的一条检查。确定性执行，不调模型。

    expect:
      exit_zero        —— command 退出码为 0
      stdout_contains  —— command 的 stdout 含 value
      commands_agree   —— command 与 value 两条命令的 stdout 完全一致
                          （对应部署 runbook 里「本地镜像 ID == 容器镜像 ID」这类核对）
    """

    name: str
    command: str
    expect: str = "exit_zero"
    value: str = ""
    timeout_s: int = 300


@dataclass(frozen=True)
class Task:
    task_id: str
    prompt: str
    spec_ref: tuple[str, ...] = ()
    declared_paths: tuple[str, ...] = ()
    declared_ops: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    max_rounds: int = 3

    @classmethod
    def from_yaml(cls, path: str | Path) -> Task:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            task_id=doc["task_id"],
            prompt=doc["prompt"],
            spec_ref=tuple(doc.get("spec_ref", ())),
            declared_paths=tuple(doc.get("declared_paths", ())),
            declared_ops=tuple(doc.get("declared_ops", ())),
            checks=tuple(
                CheckSpec(
                    name=c["name"],
                    command=c["command"],
                    expect=c.get("expect", "exit_zero"),
                    value=c.get("value", ""),
                    timeout_s=int(c.get("timeout_s", 300)),
                )
                for c in doc.get("checks", ())
            ),
            max_rounds=int(doc.get("max_rounds", 3)),
        )
```

- [x] 实现 `factory/harness/base.py`（`factory/harness/__init__.py` 留空）

```python
"""harness 适配契约。spec §7.1。

刻意保持小：一个 run()。换 harness 只需要再实现一次这个 Protocol，
编排层、监工层、审计层都不用动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from factory.task import Task


class ExitStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ToolCall:
    name: str
    call_id: str
    is_error: bool = False


@dataclass(frozen=True)
class Limits:
    max_turns: int | None = None
    timeout_s: int = 900


@dataclass(frozen=True)
class AttemptResult:
    exit_status: ExitStatus
    diff: str = ""
    diff_hash: str | None = None
    changed_paths: tuple[str, ...] = ()
    transcript_path: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_clock_ms: int = 0
    session_id: str | None = None
    harness_version: str = "unknown"
    error_text: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_status == ExitStatus.OK


class HarnessAdapter(Protocol):
    name: str

    def run(
        self,
        task: Task,
        workspace: Path,
        limits: Limits,
        *,
        model: str | None = None,
    ) -> AttemptResult: ...
```

- [x] 实现 `factory/harness/workspace.py`

```python
"""workspace diff 捕获。

用 `git add -A -N` + `git diff HEAD`：
  - -N 只登记 intent-to-add，不 stage 内容 → 无副作用，人后续照常 commit
  - 这样才能拿到「新增文件」和「新目录里的新文件」的内容，单纯 git diff 拿不到
前提：workspace 至少有 1 个 commit，否则 HEAD 不存在。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True
    )


def has_baseline(root: Path) -> bool:
    return _git(root, "rev-parse", "--verify", "HEAD").returncode == 0


def head_commit(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def capture_diff(root: Path) -> tuple[str, tuple[str, ...]]:
    if not has_baseline(root):
        raise RuntimeError(
            f"{root} 没有任何 commit，无法 diff。先 git commit 一个基线。"
        )
    _git(root, "add", "-A", "-N")
    diff = _git(root, "diff", "HEAD").stdout
    names = _git(root, "diff", "HEAD", "--name-only").stdout
    paths = tuple(line for line in names.splitlines() if line.strip())
    return diff, paths


def diff_hash(diff: str) -> str | None:
    if not diff:
        return None
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()
```

- [x] 运行 `uv run pytest tests/test_task.py tests/test_workspace.py` — 全过
- [x] 提交：`git add -A && git commit -m "feat(harness): 任务定义、diff 捕获、adapter 契约"`

---

### Task 5: transcript 解析 + ClaudeCodeAdapter

**Files:**
- Create: `factory/harness/transcript.py`, `factory/harness/claude_code.py`
- Test: `tests/test_transcript.py`, `tests/test_claude_code.py`

**Interfaces:**

Consumes: `factory.harness.base`（`AttemptResult`/`ExitStatus`/`Limits`/`ToolCall`）、
`factory.harness.workspace.capture_diff`、`factory.task.Task`

Produces:
```python
# factory/harness/transcript.py
def projects_root() -> Path                              # ~/.claude/projects
def find_transcript(session_id: str, root: Path | None = None) -> Path | None
def parse_tool_calls(path: Path) -> tuple[ToolCall, ...]

# factory/harness/claude_code.py
class ClaudeCodeAdapter:
    name = "claude_code"
    def __init__(self, binary: str = "claude",
                 projects_root: Path | None = None) -> None
    def version(self) -> str
    def run(self, task, workspace, limits, *, model=None) -> AttemptResult
```

**Steps:**

- [x] 写失败测试 `tests/test_transcript.py`

```python
import json

from factory.harness.transcript import find_transcript, parse_tool_calls


def _write_jsonl(path, records):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_find_transcript_by_glob_ignores_slug(tmp_path):
    """slug 是有损的（_ → -，中文整段变 -），所以只能按 session-id 全局 glob。"""
    d = tmp_path / "-private-tmp-probe-adapter"
    d.mkdir()
    target = d / "abc-123.jsonl"
    target.write_text("", encoding="utf-8")
    assert find_transcript("abc-123", root=tmp_path) == target


def test_find_transcript_missing(tmp_path):
    assert find_transcript("nope", root=tmp_path) is None


def test_parse_tool_calls_pairs_results(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "queue-operation"},
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "tooluse_1", "name": "Bash"},
            {"type": "tool_use", "id": "tooluse_2", "name": "Write"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tooluse_1",
             "is_error": False},
            {"type": "tool_result", "tool_use_id": "tooluse_2",
             "is_error": True},
        ]}},
        {"type": "last-prompt"},
    ])
    calls = parse_tool_calls(p)
    assert [c.name for c in calls] == ["Bash", "Write"]
    assert [c.is_error for c in calls] == [False, True]
    assert calls[0].call_id == "tooluse_1"


def test_parse_tool_calls_tolerates_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        'not json\n'
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read"}]}})
        + "\n\n",
        encoding="utf-8",
    )
    assert [c.name for c in parse_tool_calls(p)] == ["Read"]


def test_parse_tool_calls_missing_file(tmp_path):
    assert parse_tool_calls(tmp_path / "gone.jsonl") == ()
```

- [x] 写失败测试 `tests/test_claude_code.py`（用假 `claude` 脚本，不真调模型）

```python
import json
import os
import stat
import subprocess

import pytest

from factory.harness.base import ExitStatus, Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.task import Task


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    return ws


def _fake_claude(tmp_path, payload: dict, *, body: str = "") -> str:
    """造一个假 claude：把 argv 落盘、可选改 workspace、打印一段 JSON。"""
    script = tmp_path / "fake_claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, pathlib\n"
        f"argv_log = pathlib.Path({str(tmp_path / 'argv.json')!r})\n"
        "argv_log.write_text(json.dumps(sys.argv[1:]))\n"
        f"{body}\n"
        f"print(json.dumps({payload!r}))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


OK_PAYLOAD = {
    "is_error": False,
    "subtype": "success",
    "session_id": "sess-abc",
    "num_turns": 3,
    "total_cost_usd": 0.0042,
    "duration_ms": 5100,
    "usage": {"input_tokens": 120, "output_tokens": 340,
              "cache_creation_input_tokens": 10,
              "cache_read_input_tokens": 20},
    "result": "done",
}


def test_run_success_captures_everything(tmp_path, repo):
    body = (
        f"pathlib.Path({str(repo / 'greet.py')!r})"
        ".write_text('def greet(n):\\n    return n\\n')\n"
    )
    binary = _fake_claude(tmp_path, OK_PAYLOAD, body=body)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")

    res = adapter.run(
        Task(task_id="T-1", prompt="add greet"),
        repo,
        Limits(max_turns=5, timeout_s=30),
        model="haiku",
    )

    assert res.exit_status == ExitStatus.OK
    assert res.ok is True
    assert res.changed_paths == ("greet.py",)
    assert "def greet" in res.diff
    assert res.diff_hash and len(res.diff_hash) == 64
    assert res.session_id == "sess-abc"
    assert (res.tokens_in, res.tokens_out) == (120, 340)
    assert res.cost_usd == pytest.approx(0.0042)
    assert res.wall_clock_ms == 5100


def test_run_passes_model_and_max_turns(tmp_path, repo):
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    adapter.run(Task(task_id="T-1", prompt="hi"), repo,
                Limits(max_turns=7), model="sonnet")

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_run_omits_optional_flags_when_unset(tmp_path, repo):
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "--max-turns" not in argv
    assert "--model" not in argv


def test_is_error_true_maps_to_error_even_though_exit_code_is_zero(
    tmp_path, repo
):
    """claude -p 的退出码永远是 0，成功与否只能读 is_error。"""
    payload = {**OK_PAYLOAD, "is_error": True,
               "subtype": "error_max_turns", "result": "turn limit"}
    binary = _fake_claude(tmp_path, payload)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    assert res.exit_status == ExitStatus.ERROR
    assert res.ok is False
    assert "error_max_turns" in res.error_text


def test_unparseable_stdout_is_error(tmp_path, repo):
    script = tmp_path / "noisy_claude"
    script.write_text(
        "#!/usr/bin/env python3\nprint('not json at all')\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    adapter = ClaudeCodeAdapter(binary=str(script),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())
    assert res.exit_status == ExitStatus.ERROR
    assert "not json" in res.error_text


def test_timeout_maps_to_timeout_status(tmp_path, repo):
    script = tmp_path / "slow_claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    adapter = ClaudeCodeAdapter(binary=str(script),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo,
                     Limits(timeout_s=1))
    assert res.exit_status == ExitStatus.TIMEOUT


def test_finds_transcript_and_tool_calls(tmp_path, repo):
    proj = tmp_path / "proj" / "-some-lossy-slug"
    proj.mkdir(parents=True)
    (proj / "sess-abc.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit"}]}}) + "\n",
        encoding="utf-8",
    )
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    assert res.transcript_path == str(proj / "sess-abc.jsonl")
    assert [c.name for c in res.tool_calls] == ["Edit"]
```

- [x] 运行 `uv run pytest tests/test_transcript.py tests/test_claude_code.py` — 确认失败

- [x] 实现 `factory/harness/transcript.py`

```python
"""transcript 定位与解析。

原生 OTel 只导出元数据，不含对话内容；真正的审计钩子是
~/.claude/projects/<slug>/<session-id>.jsonl。

slug 由 cwd 生成且**有损**（`_` → `-`，中文目录整段变成 `-`），
从 cwd 反推 slug 不可靠，所以按 session-id 全局 glob。
"""

from __future__ import annotations

import json
from pathlib import Path

from factory.harness.base import ToolCall


def projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def find_transcript(session_id: str, root: Path | None = None) -> Path | None:
    base = root or projects_root()
    if not base.exists():
        return None
    for candidate in base.glob(f"*/{session_id}.jsonl"):
        return candidate
    return None


def parse_tool_calls(path: Path) -> tuple[ToolCall, ...]:
    """重建工具调用序列。

    assistant 记录里的 tool_use 块给出调用，紧随其后的 user 记录里的
    tool_result 块（按 tool_use_id 关联）给出成败。
    """
    if not path or not Path(path).exists():
        return ()

    calls: list[ToolCall] = []
    errors: dict[str, bool] = {}

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls.append(ToolCall(
                    name=block.get("name", "unknown"),
                    call_id=block.get("id", ""),
                ))
            elif block.get("type") == "tool_result":
                errors[block.get("tool_use_id", "")] = bool(
                    block.get("is_error", False)
                )

    return tuple(
        ToolCall(name=c.name, call_id=c.call_id,
                 is_error=errors.get(c.call_id, False))
        for c in calls
    )
```

- [x] 实现 `factory/harness/claude_code.py`

```python
"""Claude Code adapter。P0 只接这一个 harness。

两个反直觉的地方，改动前先看 Global Constraints：
  1. `claude -p` 的退出码永远是 0 —— 只读 JSON 里的 is_error
  2. `--max-turns` 能用但 `--help` 里没有 —— 未文档化依赖，所以做成可选
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from factory.harness.base import AttemptResult, ExitStatus, Limits, ToolCall
from factory.harness.transcript import find_transcript, parse_tool_calls
from factory.harness.workspace import capture_diff, diff_hash
from factory.task import Task


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(
        self, binary: str = "claude", projects_root: Path | None = None
    ) -> None:
        self._binary = binary
        self._projects_root = projects_root

    def version(self) -> str:
        try:
            proc = subprocess.run(
                [self._binary, "--version"],
                capture_output=True, text=True, timeout=30,
            )
            return proc.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def _argv(self, task: Task, limits: Limits, model: str | None) -> list[str]:
        argv = [
            self._binary, "-p", task.prompt,
            "--output-format", "json",
            "--permission-mode", "acceptEdits",
        ]
        if model:
            argv += ["--model", model]
        if limits.max_turns is not None:
            # 未文档化的 flag：--help 里没有，但 CLI 2.1.223 接受。
            # 若哪天报 unknown option，删掉这两行即可降级，不影响正确性。
            argv += ["--max-turns", str(limits.max_turns)]
        return argv

    def run(
        self,
        task: Task,
        workspace: Path,
        limits: Limits,
        *,
        model: str | None = None,
    ) -> AttemptResult:
        version = self.version()
        started = time.monotonic()

        try:
            proc = subprocess.run(
                self._argv(task, limits, model),
                cwd=workspace, capture_output=True, text=True,
                timeout=limits.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return self._result(
                workspace, ExitStatus.TIMEOUT, version,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_text=f"timeout after {limits.timeout_s}s",
            )
        except OSError as exc:
            return self._result(
                workspace, ExitStatus.ERROR, version,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_text=f"cannot launch {self._binary}: {exc}",
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return self._result(
                workspace, ExitStatus.ERROR, version, elapsed_ms=elapsed_ms,
                error_text=(proc.stdout or proc.stderr or "")[:2000],
            )

        # 退出码不可信，只看 is_error
        status = (
            ExitStatus.ERROR if payload.get("is_error")
            else ExitStatus.OK
        )
        error_text = ""
        if status == ExitStatus.ERROR:
            error_text = " ".join(
                str(payload.get(k, "")) for k in
                ("subtype", "stop_reason", "terminal_reason", "result")
            ).strip()

        usage = payload.get("usage") or {}
        session_id = payload.get("session_id")
        transcript = (
            find_transcript(session_id, root=self._projects_root)
            if session_id else None
        )
        tool_calls: tuple[ToolCall, ...] = (
            parse_tool_calls(transcript) if transcript else ()
        )

        return self._result(
            workspace, status, version,
            elapsed_ms=payload.get("duration_ms") or elapsed_ms,
            error_text=error_text,
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
            cost_usd=float(payload.get("total_cost_usd", 0.0) or 0.0),
            session_id=session_id,
            transcript_path=str(transcript) if transcript else None,
            tool_calls=tool_calls,
        )

    def _result(
        self,
        workspace: Path,
        status: ExitStatus,
        version: str,
        *,
        elapsed_ms: int,
        error_text: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        session_id: str | None = None,
        transcript_path: str | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> AttemptResult:
        """无论成败都捕获 diff —— 失败的 attempt 也可能留下改动，必须入审计。"""
        try:
            diff, paths = capture_diff(Path(workspace))
        except RuntimeError:
            diff, paths = "", ()
        return AttemptResult(
            exit_status=status,
            diff=diff,
            diff_hash=diff_hash(diff),
            changed_paths=paths,
            transcript_path=transcript_path,
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            wall_clock_ms=elapsed_ms,
            session_id=session_id,
            harness_version=version,
            error_text=error_text,
        )
```

- [x] 运行 `uv run pytest tests/test_transcript.py tests/test_claude_code.py` — 全过
- [x] 提交：`git add -A && git commit -m "feat(harness): ClaudeCodeAdapter + transcript 解析"`

---

### Task 6: 回归监工

**Files:**
- Create: `factory/supervisors/__init__.py`, `factory/supervisors/base.py`,
  `factory/supervisors/regression.py`
- Test: `tests/test_regression.py`

**Interfaces:**

Consumes: `factory.task.CheckSpec`、`factory.audit.models.{SupervisorRole, Verdict}`

Produces:
```python
# factory/supervisors/base.py
@dataclass(frozen=True)
class SupervisorReport:
    role: SupervisorRole
    verdict: Verdict
    claims: tuple[dict, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0
    @property def passed(self) -> bool

# factory/supervisors/regression.py
def run_check(spec: CheckSpec, workspace: Path) -> dict | None   # None = 通过
class RegressionSupervisor:
    role = SupervisorRole.REGRESSION
    def review(self, workspace: Path,
               checks: Sequence[CheckSpec]) -> SupervisorReport
```

**设计约束：** 回归监工是**确定性代码，零模型调用**，所以 `tokens`/`cost_usd`
恒为 0，且这一层的全部测试离线可跑。没有 check 时判 FAIL —— 「没有裁判」不等于
「通过」，这正是 A 类要求「廉价客观裁判存在」的落地点。

**Steps:**

- [x] 写失败测试 `tests/test_regression.py`

```python
import pytest

from factory.audit.models import SupervisorRole, Verdict
from factory.supervisors.regression import RegressionSupervisor, run_check
from factory.task import CheckSpec


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "greet.py").write_text("def greet(n):\n    return n\n",
                                       encoding="utf-8")
    return tmp_path


def test_exit_zero_pass(ws):
    assert run_check(CheckSpec("ok", "true"), ws) is None


def test_exit_zero_fail_records_command_and_code(ws):
    claim = run_check(CheckSpec("bad", "exit 3"), ws)
    assert claim["check"] == "bad"
    assert claim["expected"] == "exit_zero"
    assert "3" in claim["got"]


def test_stdout_contains_pass(ws):
    spec = CheckSpec("has-greet", "cat greet.py",
                     expect="stdout_contains", value="def greet")
    assert run_check(spec, ws) is None


def test_stdout_contains_fail(ws):
    spec = CheckSpec("has-farewell", "cat greet.py",
                     expect="stdout_contains", value="def farewell")
    claim = run_check(spec, ws)
    assert claim is not None
    assert "def farewell" in claim["expected"]


def test_commands_agree_pass(ws):
    spec = CheckSpec("same", "echo abc", expect="commands_agree",
                     value="echo abc")
    assert run_check(spec, ws) is None


def test_commands_agree_fail(ws):
    """部署 runbook 里的 image-id 核对：两条命令输出必须一致。"""
    spec = CheckSpec("image-id", "echo local-sha", expect="commands_agree",
                     value="echo running-sha")
    claim = run_check(spec, ws)
    assert claim is not None
    assert "local-sha" in claim["got"]
    assert "running-sha" in claim["expected"]


def test_runs_in_workspace_cwd(ws):
    spec = CheckSpec("cwd", "ls", expect="stdout_contains", value="greet.py")
    assert run_check(spec, ws) is None


def test_timeout_becomes_claim(ws):
    claim = run_check(CheckSpec("slow", "sleep 5", timeout_s=1), ws)
    assert claim is not None
    assert "timeout" in claim["got"]


def test_unknown_expect_becomes_claim(ws):
    claim = run_check(CheckSpec("weird", "true", expect="vibes"), ws)
    assert claim is not None
    assert "unknown expect" in claim["got"]


def test_review_all_pass(ws):
    report = RegressionSupervisor().review(ws, [
        CheckSpec("a", "true"),
        CheckSpec("b", "cat greet.py", expect="stdout_contains",
                  value="def greet"),
    ])
    assert report.role == SupervisorRole.REGRESSION
    assert report.verdict == Verdict.PASS
    assert report.passed is True
    assert report.claims == ()
    assert report.tokens == 0 and report.cost_usd == 0.0


def test_review_collects_every_failure_not_just_first(ws):
    report = RegressionSupervisor().review(ws, [
        CheckSpec("a", "exit 1"),
        CheckSpec("b", "true"),
        CheckSpec("c", "exit 2"),
    ])
    assert report.verdict == Verdict.FAIL
    assert [c["check"] for c in report.claims] == ["a", "c"]


def test_review_with_no_checks_fails(ws):
    """没有裁判 ≠ 通过。A 类的前提就是廉价客观裁判存在。"""
    report = RegressionSupervisor().review(ws, [])
    assert report.verdict == Verdict.FAIL
    assert report.claims[0]["check"] == "no-checks-defined"
```

- [x] 运行 `uv run pytest tests/test_regression.py` — 确认失败

- [x] 实现 `factory/supervisors/base.py`（`factory/supervisors/__init__.py` 留空）

```python
"""监工层公共类型。spec §4：三个出证据，一个出意见。

P0 只有回归监工（出证据、确定性、零模型成本）。
规格/架构监工在 P1，那时 tokens/cost_usd 才会非 0。
"""

from __future__ import annotations

from dataclasses import dataclass

from factory.audit.models import SupervisorRole, Verdict


@dataclass(frozen=True)
class SupervisorReport:
    role: SupervisorRole
    verdict: Verdict
    claims: tuple[dict, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS
```

- [x] 实现 `factory/supervisors/regression.py`

```python
"""回归监工：跑命令、比对输出。确定性，零模型调用。

claims 是打回 worker 时的唯一载荷，所以每条都必须写清
「哪个 check / 期望什么 / 实得什么」—— 含糊的 claim 会让重试轮次白烧。
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from factory.audit.models import SupervisorRole, Verdict
from factory.supervisors.base import SupervisorReport
from factory.task import CheckSpec

_MAX_CAPTURE = 2000


def _sh(command: str, workspace: Path, timeout_s: int):
    return subprocess.run(
        command, shell=True, cwd=workspace,
        capture_output=True, text=True, timeout=timeout_s,
    )


def run_check(spec: CheckSpec, workspace: Path) -> dict | None:
    """跑一条 check。通过返回 None，不通过返回一条 claim。"""
    try:
        proc = _sh(spec.command, workspace, spec.timeout_s)
    except subprocess.TimeoutExpired:
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": spec.expect,
            "got": f"timeout after {spec.timeout_s}s",
        }

    if spec.expect == "exit_zero":
        if proc.returncode == 0:
            return None
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": "exit_zero",
            "got": f"exit {proc.returncode}: "
                   f"{(proc.stderr or proc.stdout)[:_MAX_CAPTURE]}",
        }

    if spec.expect == "stdout_contains":
        if spec.value in proc.stdout:
            return None
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": f"stdout contains {spec.value!r}",
            "got": proc.stdout[:_MAX_CAPTURE],
        }

    if spec.expect == "commands_agree":
        try:
            other = _sh(spec.value, workspace, spec.timeout_s)
        except subprocess.TimeoutExpired:
            return {
                "check": spec.name,
                "command": spec.value,
                "expected": "commands_agree",
                "got": f"timeout after {spec.timeout_s}s",
            }
        if proc.stdout.strip() == other.stdout.strip():
            return None
        return {
            "check": spec.name,
            "command": f"{spec.command} vs {spec.value}",
            "expected": other.stdout.strip()[:_MAX_CAPTURE],
            "got": proc.stdout.strip()[:_MAX_CAPTURE],
        }

    return {
        "check": spec.name,
        "command": spec.command,
        "expected": spec.expect,
        "got": f"unknown expect {spec.expect!r}",
    }


class RegressionSupervisor:
    role = SupervisorRole.REGRESSION

    def review(
        self, workspace: Path, checks: Sequence[CheckSpec]
    ) -> SupervisorReport:
        if not checks:
            return SupervisorReport(
                role=self.role,
                verdict=Verdict.FAIL,
                claims=({
                    "check": "no-checks-defined",
                    "command": "",
                    "expected": "至少一条廉价客观裁判",
                    "got": "任务没有定义任何 check，不能判为通过",
                },),
            )

        claims = [c for c in (run_check(s, Path(workspace)) for s in checks)
                  if c is not None]
        return SupervisorReport(
            role=self.role,
            verdict=Verdict.FAIL if claims else Verdict.PASS,
            claims=tuple(claims),
        )
```

- [x] 运行 `uv run pytest tests/test_regression.py` — 13 个测试全过
- [x] 提交：`git add -A && git commit -m "feat(supervisors): 回归监工（确定性、零模型成本）"`

---

### Task 7: 路由表 + dispatcher

**Files:**
- Create: `factory/routing.py`, `factory/routing.yaml`, `factory/dispatcher.py`
- Test: `tests/test_routing.py`, `tests/test_dispatcher.py`

**Interfaces:**

Consumes: 前六个 Task 的全部导出

Produces:
```python
# factory/routing.py
class Router:
    def __init__(self, table: dict[str, list[str]], default: list[str]) -> None
    @classmethod def from_yaml(cls, path) -> "Router"
    @classmethod def default(cls) -> "Router"
    def model_for(self, oracle_class: OracleClass, attempt_no: int) -> str

# factory/dispatcher.py
class Outcome(StrEnum):
    MERGED = "merged"; ESCALATED = "escalated"; BLOCKED_HARD_GATE = "blocked_hard_gate"

@dataclass(frozen=True)
class DispatchReport:
    outcome: Outcome
    attempt_ids: tuple[int, ...]
    rounds: int
    final_grade: Grade
    escalation_reason: str = ""

class Dispatcher:
    def __init__(self, *, adapter: HarnessAdapter, store: AuditStore,
                 engine: GradingEngine | None = None,
                 router: Router | None = None,
                 supervisor: RegressionSupervisor | None = None,
                 limits: Limits | None = None) -> None
    def run(self, task: Task, workspace: Path) -> DispatchReport
```

**Steps:**

- [x] 写失败测试 `tests/test_routing.py`

```python
from factory.audit.models import OracleClass
from factory.routing import Router


def test_model_escalates_with_attempt_no():
    r = Router({"A": ["haiku", "sonnet", "opus"]}, default=["sonnet"])
    assert r.model_for(OracleClass.A, 1) == "haiku"
    assert r.model_for(OracleClass.A, 2) == "sonnet"
    assert r.model_for(OracleClass.A, 3) == "opus"


def test_model_clamps_beyond_ladder():
    r = Router({"A": ["haiku", "opus"]}, default=["sonnet"])
    assert r.model_for(OracleClass.A, 9) == "opus"


def test_unknown_class_uses_default():
    r = Router({}, default=["sonnet"])
    assert r.model_for(OracleClass.B, 1) == "sonnet"


def test_from_yaml(tmp_path):
    p = tmp_path / "routing.yaml"
    p.write_text(
        "default: [sonnet]\nby_class:\n  A: [haiku, sonnet]\n  B: [sonnet, opus]\n",
        encoding="utf-8",
    )
    r = Router.from_yaml(p)
    assert r.model_for(OracleClass.A, 1) == "haiku"
    assert r.model_for(OracleClass.B, 2) == "opus"


def test_builtin_table_starts_cheap_for_class_a():
    r = Router.default()
    assert r.model_for(OracleClass.A, 1) == "haiku"
    assert r.model_for(OracleClass.A, 3) == "opus"
```

- [x] 写失败测试 `tests/test_dispatcher.py`（用假 adapter，完全离线）

```python
from dataclasses import replace
from pathlib import Path

import pytest

from factory.audit.models import OracleClass, Resolution, SupervisorRole
from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.grading.rules import GradingEngine, Rule
from factory.harness.base import AttemptResult, ExitStatus, Limits
from factory.task import CheckSpec, Task


class FakeAdapter:
    """按脚本返回预设结果，并记录每轮收到的 prompt 和 model。"""

    name = "fake"

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, str | None]] = []

    def run(self, task, workspace, limits, *, model=None):
        self.calls.append((task.prompt, model))
        return self._results[min(len(self.calls) - 1, len(self._results) - 1)]


def _result(paths=("greet.py",), status=ExitStatus.OK):
    return AttemptResult(
        exit_status=status,
        diff="--- a/greet.py\n+++ b/greet.py\n+def greet(n): return n\n",
        diff_hash="a" * 64,
        changed_paths=tuple(paths),
        transcript_path="/tmp/s.jsonl",
        tokens_in=10, tokens_out=20, cost_usd=0.001, wall_clock_ms=1500,
        session_id="sess-1", harness_version="2.1.223",
    )


class AlwaysPass:
    role = SupervisorRole.REGRESSION

    def review(self, workspace, checks):
        from factory.audit.models import Verdict
        from factory.supervisors.base import SupervisorReport
        return SupervisorReport(role=self.role, verdict=Verdict.PASS)


class FailsThenPasses:
    role = SupervisorRole.REGRESSION

    def __init__(self, fail_times: int):
        self._left = fail_times
        self.seen = 0

    def review(self, workspace, checks):
        from factory.audit.models import Verdict
        from factory.supervisors.base import SupervisorReport
        self.seen += 1
        if self._left > 0:
            self._left -= 1
            return SupervisorReport(
                role=self.role, verdict=Verdict.FAIL,
                claims=({"check": "pytest", "expected": "exit_zero",
                         "got": "exit 1: 2 failed"},),
            )
        return SupervisorReport(role=self.role, verdict=Verdict.PASS)


@pytest.fixture
def store():
    return AuditStore(":memory:")


def _task(**kw):
    base = dict(task_id="T-1", prompt="add greet", spec_ref=("AC-1",),
                declared_paths=("greet.py",),
                checks=(CheckSpec("pytest", "true"),))
    return Task(**{**base, **kw})


def _dispatcher(store, adapter, supervisor=None, engine=None):
    return Dispatcher(
        adapter=adapter, store=store,
        engine=engine or GradingEngine.default(),
        supervisor=supervisor or AlwaysPass(),
        limits=Limits(max_turns=5, timeout_s=60),
    )


def test_class_a_all_green_merges_on_first_round(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.MERGED
    assert report.rounds == 1
    assert len(report.attempt_ids) == 1

    row = store.get(report.attempt_ids[0])
    assert row.resolution == Resolution.MERGED
    assert row.oracle_class == OracleClass.A
    assert row.spec_ref == ["AC-1"]
    assert row.harness == "fake"
    assert row.harness_version == "2.1.223"
    assert row.model == "haiku"
    assert row.diff_hash == "a" * 64
    assert row.transcript_path == "/tmp/s.jsonl"
    assert (row.tokens_in, row.tokens_out) == (10, 20)
    assert row.wall_clock_ms == 1500
    assert len(row.supervisors) == 1


def test_failure_sends_claims_back_and_escalates_model(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, FailsThenPasses(1)).run(
        _task(), tmp_path
    )

    assert report.outcome == Outcome.MERGED
    assert report.rounds == 2
    # 第二轮的 prompt 必须带上具体失败项
    assert "exit 1: 2 failed" in adapter.calls[1][0]
    # 第二轮换更强的模型
    assert [m for _, m in adapter.calls] == ["haiku", "sonnet"]
    assert store.get(report.attempt_ids[0]).resolution == Resolution.REWORKED
    assert store.get(report.attempt_ids[1]).resolution == Resolution.MERGED


def test_three_strikes_escalates_to_human(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, FailsThenPasses(99)).run(
        _task(), tmp_path
    )

    assert report.outcome == Outcome.ESCALATED
    assert report.rounds == 3
    assert len(report.attempt_ids) == 3
    assert store.get(report.attempt_ids[-1]).resolution == Resolution.ESCALATED


def test_pre_dispatch_class_c_never_runs_the_agent(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter).run(
        _task(declared_paths=("src/auth/login.py",)), tmp_path
    )

    assert report.outcome == Outcome.ESCALATED
    assert adapter.calls == []          # 一次都没派发
    assert report.final_grade.oracle_class == OracleClass.C
    row = store.get(report.attempt_ids[0])
    assert row.resolution == Resolution.ESCALATED
    assert "C:" in row.class_reason


def test_pre_dispatch_class_d_is_hard_gate(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter).run(
        _task(declared_ops=("prod_deploy",)), tmp_path
    )

    assert report.outcome == Outcome.BLOCKED_HARD_GATE
    assert adapter.calls == []
    assert store.get(report.attempt_ids[0]).oracle_class == OracleClass.D


def test_post_diff_escalation_when_agent_touched_worse_paths(store, tmp_path):
    """预分级 A，实际改了 migrations/ → 必须升级，且不能 merge。"""
    adapter = FakeAdapter([_result(paths=("greet.py", "db/migrations/007.py"))])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.BLOCKED_HARD_GATE
    assert report.final_grade.oracle_class == OracleClass.D
    row = store.get(report.attempt_ids[0])
    assert row.oracle_class == OracleClass.D
    assert "db/migrations/007.py" in row.class_reason
    assert row.resolution == Resolution.ESCALATED
    # 后分级独立成一条 risk 记录，便于两周后算命中率
    roles = {v.role for v in row.supervisors}
    assert SupervisorRole.RISK in roles


def test_post_diff_same_severity_does_not_escalate(store, tmp_path):
    adapter = FakeAdapter([_result(paths=("greet.py", "utils.py"))])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)
    assert report.outcome == Outcome.MERGED
    assert report.final_grade.oracle_class == OracleClass.A


def test_harness_error_counts_as_a_failed_round(store, tmp_path):
    adapter = FakeAdapter([replace(_result(), exit_status=ExitStatus.ERROR,
                                   error_text="error_max_turns")])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.ESCALATED
    assert report.rounds == 3
    claims = store.get(report.attempt_ids[0]).supervisors[0].claims
    assert any("error_max_turns" in str(c) for c in claims)


def test_empty_diff_is_a_failed_round(store, tmp_path):
    adapter = FakeAdapter([replace(_result(), diff="", diff_hash=None,
                                   changed_paths=())])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)
    assert report.outcome == Outcome.ESCALATED
    claims = store.get(report.attempt_ids[0]).supervisors[0].claims
    assert any("no changes" in str(c) for c in claims)


def test_max_rounds_from_task_is_respected(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, FailsThenPasses(99)).run(
        _task(max_rounds=1), tmp_path
    )
    assert report.rounds == 1
    assert report.outcome == Outcome.ESCALATED
```

- [x] 运行 `uv run pytest tests/test_routing.py tests/test_dispatcher.py` — 确认失败

- [x] 实现 `factory/routing.yaml`

```yaml
# 模型分级路由表。手工维护 —— 这是有意的选择：
# 榜单和发布时间由人看，表由人改，代码不猜。
#
# 列表 = 重试阶梯：第 1 轮用第 1 个，第 2 轮用第 2 个，超出则钉在最后一个。
# 便宜的先上，打不过再换强的。
default: [sonnet]
by_class:
  A: [haiku, sonnet, opus]
  B: [sonnet, opus, opus]
  # C/D 永不无人，这里留着只为「万一人工放行后仍走本管道」时有个强模型兜底
  C: [opus]
  D: [opus]
```

- [x] 实现 `factory/routing.py`

```python
"""模型分级路由。第一轴是模型档位，第二轴（换 harness）留给 P1。"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.audit.models import OracleClass


class Router:
    def __init__(self, table: dict[str, list[str]], default: list[str]) -> None:
        self._table = {str(k): list(v) for k, v in (table or {}).items()}
        self._default = list(default) or ["sonnet"]

    @classmethod
    def from_yaml(cls, path: str | Path) -> Router:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(doc.get("by_class", {}), doc.get("default", ["sonnet"]))

    @classmethod
    def default(cls) -> Router:
        return cls.from_yaml(Path(__file__).with_name("routing.yaml"))

    def model_for(self, oracle_class: OracleClass, attempt_no: int) -> str:
        """attempt_no 从 1 起。超出阶梯长度则钉在最后一档（最强的那个）。"""
        ladder = self._table.get(str(oracle_class.value), self._default)
        idx = max(0, min(attempt_no - 1, len(ladder) - 1))
        return ladder[idx]
```

- [x] 实现 `factory/dispatcher.py`

```python
"""编排循环。spec §4.2：全绿 → 合并；有红 → 带具体失败项打回，最多 3 轮；
3 轮不过 → 升级给人。

三条不可协商的路径（对应 Global Constraints 9/10）：
  - 预分级 C → 一次都不派发，直接升级给人
  - 预分级 D → 硬闸门，不派发，只落审计（agent 只能生成脚本，不执行）
  - 后分级比预分级严重 → 改写 oracle_class；升到 C/D 就不许 merge

「后分级」就是 spec §4 里的风险监工，所以它以一条 role=risk 的
SupervisorVerdict 落库 —— 两周后算命中率时它和别的监工同一张表。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from factory.audit.models import (
    OracleClass, Resolution, SupervisorRole, Verdict,
)
from factory.audit.store import AuditStore
from factory.grading.rules import Grade, GradingEngine
from factory.harness.base import HarnessAdapter, Limits
from factory.routing import Router
from factory.supervisors.base import SupervisorReport
from factory.supervisors.regression import RegressionSupervisor
from factory.task import Task


class Outcome(StrEnum):
    MERGED = "merged"
    ESCALATED = "escalated"
    BLOCKED_HARD_GATE = "blocked_hard_gate"


@dataclass(frozen=True)
class DispatchReport:
    outcome: Outcome
    attempt_ids: tuple[int, ...]
    rounds: int
    final_grade: Grade
    escalation_reason: str = ""


class Dispatcher:
    def __init__(
        self,
        *,
        adapter: HarnessAdapter,
        store: AuditStore,
        engine: GradingEngine | None = None,
        router: Router | None = None,
        supervisor: RegressionSupervisor | None = None,
        limits: Limits | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._engine = engine or GradingEngine.default()
        self._router = router or Router.default()
        self._supervisor = supervisor or RegressionSupervisor()
        self._limits = limits or Limits()

    # ---------- 预分级：不通过就一次都不派发 ----------

    def _record_blocked(self, task: Task, grade: Grade, model: str) -> int:
        aid = self._store.open_attempt(
            task_id=task.task_id,
            spec_ref=list(task.spec_ref),
            oracle_class=grade.oracle_class,
            class_reason=grade.reason,
            harness=self._adapter.name,
            harness_version="n/a",
            model=model,
        )
        self._store.record_verdict(
            aid,
            role=SupervisorRole.RISK,
            verdict=Verdict.FAIL,
            claims=[{
                "check": "pre-dispatch-grading",
                "command": "",
                "expected": "class A/B (unmanned allowed)",
                "got": grade.reason,
            }],
        )
        self._store.finalize(aid, Resolution.ESCALATED)
        return aid

    def run(self, task: Task, workspace: Path) -> DispatchReport:
        pre = self._engine.grade(task.declared_paths, task.declared_ops)

        if not pre.unmanned_allowed:
            model = self._router.model_for(pre.oracle_class, 1)
            aid = self._record_blocked(task, pre, model)
            outcome = (
                Outcome.BLOCKED_HARD_GATE if pre.hard_gate
                else Outcome.ESCALATED
            )
            reason = (
                f"pre-dispatch {pre.oracle_class.value}: {pre.reason}"
                + (" —— 硬闸门，agent 只能生成待执行脚本" if pre.hard_gate else "")
            )
            return DispatchReport(outcome, (aid,), 0, pre, reason)

        return self._loop(task, Path(workspace), pre)

    # ---------- 主循环 ----------

    def _loop(self, task: Task, workspace: Path, pre: Grade) -> DispatchReport:
        attempt_ids: list[int] = []
        feedback: tuple[dict, ...] = ()
        grade = pre

        for round_no in range(1, task.max_rounds + 1):
            model = self._router.model_for(pre.oracle_class, round_no)
            aid = self._store.open_attempt(
                task_id=task.task_id,
                spec_ref=list(task.spec_ref),
                oracle_class=pre.oracle_class,
                class_reason=pre.reason,
                harness=self._adapter.name,
                harness_version="pending",
                model=model,
            )
            attempt_ids.append(aid)

            result = self._adapter.run(
                replace(task, prompt=self._prompt(task, feedback)),
                workspace,
                self._limits,
                model=model,
            )
            self._store.record_result(
                aid,
                diff_hash=result.diff_hash,
                commit=None,
                transcript_path=result.transcript_path,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
                wall_clock_ms=result.wall_clock_ms,
                harness_version=result.harness_version,
            )

            # 后分级：用真实改动的文件再判一次
            post = self._engine.grade(result.changed_paths, task.declared_ops)
            if post.more_severe_than(grade):
                grade = post
                escalated_reason = f"post-diff escalation: {post.reason}"
                self._store.escalate_class(
                    aid, oracle_class=post.oracle_class,
                    class_reason=escalated_reason,
                )
                self._store.record_verdict(
                    aid, role=SupervisorRole.RISK, verdict=Verdict.FAIL,
                    claims=[{
                        "check": "post-diff-grading",
                        "command": "",
                        "expected": f"class {pre.oracle_class.value} "
                                    f"(declared)",
                        "got": post.reason,
                    }],
                )
                if not post.unmanned_allowed:
                    self._store.finalize(aid, Resolution.ESCALATED)
                    outcome = (
                        Outcome.BLOCKED_HARD_GATE if post.hard_gate
                        else Outcome.ESCALATED
                    )
                    return DispatchReport(
                        outcome, tuple(attempt_ids), round_no, post,
                        escalated_reason,
                    )
            else:
                self._store.record_verdict(
                    aid, role=SupervisorRole.RISK, verdict=Verdict.PASS,
                    claims=[],
                )

            report = self._review(task, workspace, result)
            self._store.record_verdict(
                aid, role=report.role, verdict=report.verdict,
                claims=list(report.claims),
                tokens=report.tokens, cost_usd=report.cost_usd,
            )

            if report.passed:
                self._store.finalize(aid, Resolution.MERGED)
                return DispatchReport(
                    Outcome.MERGED, tuple(attempt_ids), round_no, grade
                )

            feedback = report.claims
            is_last = round_no == task.max_rounds
            self._store.finalize(
                aid, Resolution.ESCALATED if is_last else Resolution.REWORKED
            )

        return DispatchReport(
            Outcome.ESCALATED, tuple(attempt_ids), task.max_rounds, grade,
            f"{task.max_rounds} 轮未通过，升级给人：" + self._render(feedback),
        )

    # ---------- 监工调用 ----------

    def _review(self, task, workspace, result) -> SupervisorReport:
        """harness 报错 / 空 diff 都算这一轮红，且不去跑 check。

        原因：check 全绿但 agent 什么都没改，说明 check 太弱或任务已完成，
        两种都需要人看一眼，不能静默 merge。
        """
        if not result.ok:
            return SupervisorReport(
                role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
                claims=({
                    "check": "harness",
                    "command": self._adapter.name,
                    "expected": "exit_status ok",
                    "got": f"{result.exit_status.value}: {result.error_text}",
                },),
            )
        if not result.changed_paths:
            return SupervisorReport(
                role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
                claims=({
                    "check": "diff",
                    "command": "git diff HEAD",
                    "expected": "至少一个文件改动",
                    "got": "no changes produced",
                },),
            )
        return self._supervisor.review(workspace, task.checks)

    # ---------- 打回时的 prompt ----------

    @staticmethod
    def _render(claims) -> str:
        return "\n".join(
            f"- [{c.get('check')}] 期望 {c.get('expected')!r}，"
            f"实得 {c.get('got')!r}"
            for c in claims
        )

    def _prompt(self, task: Task, feedback: tuple[dict, ...]) -> str:
        if not feedback:
            return task.prompt
        return (
            f"{task.prompt}\n\n"
            "上一轮被回归监工打回。以下是具体失败项，逐条修掉，不要改动无关文件：\n"
            f"{self._render(feedback)}\n"
        )
```

- [x] `record_result` 多了 `harness_version` 参数 —— 回 Task 2 给 `AuditStore.record_result`
  加上 `harness_version: str | None = None`（非 None 时才覆盖写），并给
  `tests/test_audit_store.py` 补一条：

```python
def test_record_result_updates_harness_version(store):
    aid = _open(store)
    store.record_result(
        aid, diff_hash=None, commit=None, transcript_path=None,
        tokens_in=0, tokens_out=0, cost_usd=0.0, wall_clock_ms=0,
        harness_version="2.1.223",
    )
    assert store.get(aid).harness_version == "2.1.223"
```

- [x] 运行 `uv run pytest tests/test_routing.py tests/test_dispatcher.py tests/test_audit_store.py` — 全过
- [x] 提交：`git add -A && git commit -m "feat(dispatcher): 编排循环 + 三轮重试 + 双次分级硬闸门"`

---

### Task 8: CLI 入口 + 端到端

**Files:**
- Create: `factory/cli.py`
- Create: `examples/greet_task.yaml`
- Test: `tests/test_cli.py`, `tests/test_e2e_smoke.py`

**Interfaces:**

Consumes: 全部前序模块

Produces:
```python
# factory/cli.py
def build_dispatcher(db_path, *, binary="claude", timeout_s=900,
                     max_turns=None) -> Dispatcher
def main(argv: list[str] | None = None) -> int   # 0=merged，1=其他
```

CLI 形态：
```
python -m factory.cli run <task.yaml> --workspace <dir> [--db audit.db]
                                      [--max-turns N] [--timeout 900]
python -m factory.cli show <task_id> --db audit.db
```

**Steps:**

- [x] 写 `examples/greet_task.yaml`

```yaml
task_id: T-greet-1
prompt: |
  在仓库根目录创建 greet.py，实现 greet(name) -> str，返回 "Hello, {name}!"。
  同时创建 tests/test_greet.py 覆盖正常入参。不要改动其他文件。
spec_ref: [AC-1]
declared_paths: ["greet.py", "tests/test_greet.py"]
declared_ops: []
max_rounds: 3
checks:
  - name: greet-exists
    command: test -f greet.py
  - name: greet-returns-expected
    command: python -c "import greet; print(greet.greet('world'))"
    expect: stdout_contains
    value: "Hello, world!"
  - name: pytest-green
    command: python -m pytest -q
```

- [x] 写失败测试 `tests/test_cli.py`（离线，用假 claude）

```python
import json
import stat
import subprocess
import sys

import pytest

from factory.audit.store import AuditStore
from factory.cli import main


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    return ws


@pytest.fixture
def task_file(tmp_path):
    p = tmp_path / "task.yaml"
    p.write_text(
        "task_id: T-cli-1\n"
        "prompt: create greet.py\n"
        "spec_ref: [AC-1]\n"
        "declared_paths: ['greet.py']\n"
        "checks:\n"
        "  - name: greet-exists\n"
        "    command: test -f greet.py\n",
        encoding="utf-8",
    )
    return p


def _fake_claude(tmp_path, repo):
    script = tmp_path / "fake_claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(repo / 'greet.py')!r}).write_text("
        "'def greet(n):\\n    return f\"Hello, {n}!\"\\n')\n"
        "print(json.dumps({'is_error': False, 'session_id': 'sess-cli',\n"
        "  'total_cost_usd': 0.001, 'duration_ms': 900,\n"
        "  'usage': {'input_tokens': 5, 'output_tokens': 7}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_run_merges_and_returns_zero(tmp_path, repo, task_file, capsys):
    db = tmp_path / "audit.db"
    code = main([
        "run", str(task_file), "--workspace", str(repo),
        "--db", str(db), "--binary", _fake_claude(tmp_path, repo),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "merged" in out

    row = AuditStore(db).get(1)
    assert row.task_id == "T-cli-1"
    assert row.resolution == "merged"
    assert row.model == "haiku"
    assert row.diff_hash is not None


def test_run_hard_gate_returns_nonzero(tmp_path, repo, capsys):
    p = tmp_path / "d.yaml"
    p.write_text(
        "task_id: T-d-1\nprompt: deploy it\n"
        "declared_ops: [prod_deploy]\n"
        "checks:\n  - name: noop\n    command: 'true'\n",
        encoding="utf-8",
    )
    code = main(["run", str(p), "--workspace", str(repo),
                 "--db", str(tmp_path / "a.db")])
    assert code == 1
    out = capsys.readouterr().out
    assert "blocked_hard_gate" in out
    assert "硬闸门" in out


def test_show_prints_audit_trail(tmp_path, repo, task_file, capsys):
    db = tmp_path / "audit.db"
    main(["run", str(task_file), "--workspace", str(repo), "--db", str(db),
          "--binary", _fake_claude(tmp_path, repo)])
    capsys.readouterr()

    assert main(["show", "T-cli-1", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "T-cli-1" in out
    assert "regression" in out
    assert "merged" in out
```

- [x] 写 `tests/test_e2e_smoke.py`（唯一真调 `claude` 的测试）

```python
"""端到端 smoke：真的调 claude。默认不跑。

    uv run pytest -m smoke -s

这是 spec §9 P0 的完成判据：一个 A 类任务端到端跑通，审计记录字段完整。
"""

import shutil
import subprocess

import pytest

from factory.audit.store import AuditStore
from factory.cli import main


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI 不可用")
def test_class_a_task_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("# scratch\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")

    task = tmp_path / "task.yaml"
    task.write_text(
        "task_id: T-smoke-1\n"
        "prompt: |\n"
        "  在仓库根目录创建 greet.py，实现 greet(name) -> str，\n"
        "  返回 \"Hello, {name}!\"。不要改动其他文件。\n"
        "spec_ref: [AC-1]\n"
        "declared_paths: ['greet.py']\n"
        "max_rounds: 2\n"
        "checks:\n"
        "  - name: greet-exists\n"
        "    command: test -f greet.py\n"
        "  - name: greet-output\n"
        "    command: python -c \"import greet; print(greet.greet('world'))\"\n"
        "    expect: stdout_contains\n"
        "    value: 'Hello, world!'\n",
        encoding="utf-8",
    )

    db = tmp_path / "audit.db"
    code = main(["run", str(task), "--workspace", str(ws), "--db", str(db),
                 "--max-turns", "20"])
    assert code == 0, "A 类任务应当自动 merge"

    # 审计记录字段完整性 —— 这是完成判据的后半句
    row = AuditStore(db).get(1)
    assert row.task_id == "T-smoke-1"
    assert row.attempt_no == 1
    assert row.spec_ref == ["AC-1"]
    assert row.oracle_class == "A"
    assert row.class_reason
    assert row.harness == "claude_code"
    assert row.harness_version and row.harness_version != "unknown"
    assert row.model == "haiku"
    assert row.diff_hash and len(row.diff_hash) == 64
    assert row.transcript_path and row.transcript_path.endswith(".jsonl")
    assert row.tokens_in > 0 and row.tokens_out > 0
    assert row.cost_usd > 0
    assert row.wall_clock_ms > 0
    assert row.resolution == "merged"
    assert row.linked_defects == []
    assert row.created_at is not None
    roles = {v.role for v in row.supervisors}
    assert {"regression", "risk"} <= roles
    assert (ws / "greet.py").exists()
```

- [x] 运行 `uv run pytest tests/test_cli.py` — 确认失败

- [x] 实现 `factory/cli.py`

```python
"""CLI 入口。

    python -m factory.cli run <task.yaml> --workspace <dir> [--db audit.db]
    python -m factory.cli show <task_id> --db audit.db
"""

from __future__ import annotations

import argparse
from pathlib import Path

from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.harness.base import Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.task import Task


def build_dispatcher(
    db_path: str | Path,
    *,
    binary: str = "claude",
    timeout_s: int = 900,
    max_turns: int | None = None,
) -> Dispatcher:
    return Dispatcher(
        adapter=ClaudeCodeAdapter(binary=binary),
        store=AuditStore(db_path),
        limits=Limits(max_turns=max_turns, timeout_s=timeout_s),
    )


def _cmd_run(args) -> int:
    task = Task.from_yaml(args.task)
    dispatcher = build_dispatcher(
        args.db, binary=args.binary,
        timeout_s=args.timeout, max_turns=args.max_turns,
    )
    report = dispatcher.run(task, Path(args.workspace))

    print(f"task     : {task.task_id}")
    print(f"outcome  : {report.outcome.value}")
    print(f"rounds   : {report.rounds}")
    print(f"class    : {report.final_grade.oracle_class.value} "
          f"({report.final_grade.reason})")
    print(f"attempts : {', '.join(str(i) for i in report.attempt_ids)}")
    if report.escalation_reason:
        print(f"escalated: {report.escalation_reason}")
    if report.outcome == Outcome.BLOCKED_HARD_GATE:
        print("硬闸门：不可逆操作必须由人执行。"
              "agent 只能生成待执行脚本，本管道不代为执行。")
    return 0 if report.outcome == Outcome.MERGED else 1


def _cmd_show(args) -> int:
    store = AuditStore(args.db)
    total = store.next_attempt_no(args.task_id) - 1
    if total <= 0:
        print(f"没有 {args.task_id} 的记录")
        return 1
    for row in store.attempts_for(args.task_id):
        print(f"--- {row.task_id} attempt {row.attempt_no} ---")
        print(f"  class      : {row.oracle_class} ({row.class_reason})")
        print(f"  harness    : {row.harness} {row.harness_version} "
              f"model={row.model}")
        print(f"  diff_hash  : {row.diff_hash}")
        print(f"  transcript : {row.transcript_path}")
        print(f"  tokens     : in={row.tokens_in} out={row.tokens_out} "
              f"cost=${row.cost_usd:.4f} {row.wall_clock_ms}ms")
        print(f"  resolution : {row.resolution}")
        print(f"  defects    : {row.linked_defects}")
        for v in row.supervisors:
            print(f"  [{v.role}] {v.verdict}")
            for c in v.claims:
                print(f"      - {c.get('check')}: 期望 {c.get('expected')!r} "
                      f"/ 实得 {c.get('got')!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="跑一个任务")
    run.add_argument("task")
    run.add_argument("--workspace", required=True)
    run.add_argument("--db", default="audit.db")
    run.add_argument("--binary", default="claude")
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--max-turns", type=int, default=None)
    run.set_defaults(func=_cmd_run)

    show = sub.add_parser("show", help="打印某个任务的审计轨迹")
    show.add_argument("task_id")
    show.add_argument("--db", default="audit.db")
    show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] `show` 用到了 `AuditStore.attempts_for` —— 回 Task 2 补上，并补测试：

```python
# factory/audit/store.py
    def attempts_for(self, task_id: str) -> list[TaskAttempt]:
        with self._session() as s:
            rows = list(s.scalars(
                select(TaskAttempt)
                .options(selectinload(TaskAttempt.supervisors))
                .where(TaskAttempt.task_id == task_id)
                .order_by(TaskAttempt.attempt_no)
            ))
            s.expunge_all()
            return rows
```

```python
# tests/test_audit_store.py
def test_attempts_for_returns_in_attempt_order(store):
    _open(store, "T-1")
    _open(store, "T-1")
    _open(store, "T-2")
    rows = store.attempts_for("T-1")
    assert [r.attempt_no for r in rows] == [1, 2]
```

- [x] 运行 `uv run pytest tests/test_cli.py tests/test_audit_store.py` — 全过
- [x] 运行全量离线测试：`uv run pytest -m "not smoke"` — 全过
- [x] 提交：`git add -A && git commit -m "feat(cli): run/show 入口 + 端到端 smoke"`
- [x] 跑真实端到端：`uv run pytest -m smoke -s` — 通过即达成 spec §9 的 P0 判据
- [x] 提交：`git add -A && git commit -m "test: P0 端到端 smoke 通过"`

---

## P0 完成判据核对表

跑完 Task 8 后逐条核对，全部为真才算 P0 收口：

- [x] 一个 A 类任务从 task.yaml 到 merge 全自动跑通，人未介入
- [x] `task_attempt` 的 spec §5 字段全部有值：`task_id` / `attempt_no` / `spec_ref` /
      `oracle_class` / `class_reason` / `harness` / `harness_version` / `diff_hash` /
      `commit` / `transcript_path` / `tokens` / `cost` / `resolution`
  - 这一条**当时漏了 `commit`**。§5 的字段清单里它就在 `diff_hash` 旁边，
    但这条判据把它跳过了，于是「全部有值」在一个永远是 None 的字段上打了勾。
    2026-08-08 补上（见下文「P1 落地」）—— 判据本身漏项，比实现漏项更难发现，
    因为核对表读起来是绿的。
- [x] `commit` 提交的文件集**正好**等于监工审过的那一组（`diff_hash` 的来源），
      且主工作树未被写入 —— 这条只有真跑能查：check 命令自己会造 `__pycache__`
- [x] 短 sha 能反查回 attempt（`attempt_by_commit`），即 git blame → 缺陷现场那一跳
- [x] `supervisors[]` 至少有 regression + risk 两条，FAIL 时 `claims` 写清期望与实得
- [x] 分级引擎跑了两次，后分级更严时 `oracle_class` 被改写且 `class_reason` 可回溯
- [x] 声明 `prod_deploy` 的任务被 D 类硬闸门拦住，adapter 一次都没被调用
- [x] 声明 `*auth/*` 路径的任务被 C 类拦住，adapter 一次都没被调用
- [x] 三轮不过升级给人，且每轮 `resolution` 分别是 reworked/reworked/escalated
- [x] 除 smoke 外全部测试离线可跑（`uv run pytest -m "not smoke"` 不需要网络与 API key）
- [x] 审计库里没有任何明文密码或 API key

## 不在 P0 范围内（勿顺手做）

- ~~规格监工、架构监工（要调模型）→ P1~~ **已完成**（`factory/supervisors/`，commit `77cc039`）
  - 四独立性 flag 探针已测（`--tools ""` `--safe-mode` `--exclude-dynamic-system-prompt-sections` + 空 cwd）
  - 真跑发现两个非测试可发现的 bug，均已修并 pin：①监工 cwd 泄漏自身仓库上下文；②规格监工因差异集不含引用文件而误判
- ~~监工命中率报表（两周数据攒够后再做）→ P1~~ **已完成**（`factory/metrics.py`，三修剪指标 + 故障桶独立记账，不污染命中率分母）
- ~~多 harness（codex / pi）→ P1，接口已备好，加一个 Protocol 实现即可~~
  **已完成**（`factory/harness/shell.py`，commit `dbf6515`）——「接口已备好」现在
  被第二个实现证伪过了：Protocol 本身没问题，但两个 adapter 的成败判据是相反的
- ~~容器隔离 / worktree 并行 → P1~~ **两件都已完成**：worktree 并行（`10a0d9f`）+
  副作用隔离（`1e3fb71`，落在 Seatbelt 而不是容器，理由见下）
- ~~录音 → PRD 的入口层 → P1~~ **已完成**（`factory/intake/`，commit `2355e20`）
  - guard.py 确定性关键词扫描，declared_ops 只增不减，18 条 A 类说法 0 误报
  - 真跑：一句口述 → 7 条验收标准 → 两轮（haiku/sonnet）→ merged
  - D 类同样真跑：派发被 pre-dispatch 拦住，目标仓库 git 全干净
  - 顺带修了接口错配：Task 新增 `acceptance` 字段；`criteria = spec_ref + acceptance`
    （所有口述来源的任务 spec_ref 为空，原来规格监工永远判 supervisor-spec-no-criteria）
- PRD ↔ diff 一致性检查 → P2

---

## P1 四监工真跑记录（2026-08-06，第二次，两个 bug 修完后）

任务 `T-p1-slug`：新建 `titles.py` 实现 `make_slug`，仓库里**已有** `text.py:slugify` ——
故意埋的重复实现陷阱。`--spec-review --architecture-review --judge-model sonnet`。

| 轮 | risk | regression | spec | architecture | resolution |
|---|---|---|---|---|---|
| 1 | pass $0 | pass $0 | **fail** $0.0424 | **fail** $0.0496 | reworked |
| 2 | pass $0 | pass $0 | pass $0.1420 | **fail** $0.0442 | reworked |
| 3 | pass $0 | pass $0 | pass $0.0493 | pass $0.3213 | **merged** |

单任务总成本约 $0.65，全部落在两个模型监工上；两个确定性监工零成本。

三个值得记的结论：

1. **陷阱被第一轮抓住，两个监工独立命中同一处。** spec 从 AC-2 判据切入，
   architecture 从「重复实现」类目切入，措辞和引用位置都不同 —— 说明独立性是结构性的
   （扣输入 + 干净上下文），不是两份提示词各自复读同一句话。
2. **两个 bug 的修复都在这次生效。** 架构监工的 claim 全部指向被审仓库自己的文件
   （`titles.py` / `text.py`），不再引用编排层仓库；规格监工第二轮转 pass，因为
   `neighbour_context` 把 `text.py` 带进去了，它终于能确认 `slugify` 返回 `str`。
3. **软意见的去处按设计走了。** 第二轮 architecture 报「死代码：`make_slug` 没人调」，
   这是软意见，因为不是末轮所以打回 worker，第三轮 worker 补了 `main()` 后转 pass。
   末轮 architecture 单独 fail 不拦合并 —— 这条被 `tests/test_dispatcher_four.py` pin 住。

闸门 3（上人平均打回次数 ≤ 1）目前 **2.00，未达标**，但样本只有 1 个任务。
其中一次打回来自软意见（死代码），不是判据写错 —— 攒够样本前不动 checks。


---

## P1 worktree 隔离 + 并行派发（2026-08-07，commit `10a0d9f`）

`factory/harness/worktree.py` + CLI `run` 支持多 task YAML、`--parallel N`。

选 worktree 不选 clone/容器的理由：clone 每次拷全量对象库，大仓库上单任务就要
几十秒；worktree 共享 `.git`，创建是常数时间。容器隔离的是**副作用**（装包、改系统），
worktree 隔离的是**工作树** —— 并行派发第一个撞的是后者：两个 agent 同时改同一棵树，
diff 会互相污染，`diff_hash` 就不再对应任何一个任务的改动。容器留给 P1 后半段。

### 三个并发 bug，都是探针实测出来的，不是预防性设计

1. **`create_all` 撞车。** 8 个线程同时 `AuditStore(同一路径)` 报
   `table task_attempt already exists` —— SQLAlchemy 的 `checkfirst` 是
   「先查后建」，不是原子的。用模块级 `_SCHEMA_LOCK` 把建表串起来。
2. **并发写直接 `database is locked`。** 默认 journal 模式下读写互斥。
   改 WAL + `busy_timeout=10s`，让写写排队而不是报错。用
   `@event.listens_for(engine, "connect")` 设置，保证每条连接都生效
   （`:memory:` 不支持 WAL，要跳过，否则所有单测全挂）。
3. **`attempt_no` 撞号。** 它是「读 max 再插」，并发下两个线程会算出同一个号。
   靠 `UniqueConstraint(task_id, attempt_no)` 兜住并重试。
   **不能改成全局自增**：`attempt_no` 是「这个任务的第几轮」，`show` 和闸门 3 的
   打回计数都按它读，变成全库序号审计轨迹就没法看了。

实测：8 线程 × 12 次写 = 96 行，无丢行、无撞号、裁决全部挂在正确的 attempt 上。

### 两个刻意的默认值

- **单任务默认不开 worktree。** P0 的最便宜路径不能因为 P1 变贵，要隔离显式加
  `--worktree`。
- **跑完不删 worktree。** agent 干完活、监工判了绿，但合并是人的动作。
  自动删等于把还没人看过的产出扔了。`release(discard=False)` 见到未提交改动
  直接拒绝删并返回 `False`。

---

## P1 第二个 harness：ShellAdapter（2026-08-07，commit `dbf6515`）

`factory/harness/shell.py`。存在的理由不是「多一个选择」，而是**验证
`HarnessAdapter` 这个 Protocol 真的可替换** —— 只有一个实现时，
「接口已备好」是无法证伪的说法。

和 `ClaudeCodeAdapter` 的契约**相反**，这是接第三个 harness 前必须先分清的事：

| | 判成败靠 |
|---|---|
| `ClaudeCodeAdapter` | 退出码恒为 0，只能读 JSON 里的 `is_error` |
| `ShellAdapter` | 退出码就是真相，没有 JSON 可读 |

**刻意不用 `shell=True`。** prompt 是任务文件里的自由文本，将来还会由 PRD
生成器产出。走 shell 就等于把它当命令拼起来执行，一个反引号就能越权。
`test_prompt_with_shell_metacharacters_is_not_executed` 是这个决定的锚点。

### 真跑（2026-08-07）：一个零成本的 codemod 任务

worker 是一条确定性脚本（把模块级常量收进 frozen dataclass），根本不调模型。
`--spec-review --architecture-review --judge-model sonnet`：

| | 结果 | 成本 | 耗时 |
|---|---|---|---|
| worker（shell） | 一轮做对 | **$0** | 24 ms |
| 四监工 | 全 pass | $0.397 | ~40 s |

**验收比生产贵 —— 而且是 100% 对 0%。** 这不是异常，是 A/B 类确定性任务的常态：
能写成脚本的活，成本全在「证明它做对了」那一侧。它直接指向 spec §5.1 的修剪问题：
对这类任务，两个模型监工的单位命中成本是无穷大（零命中、$0.397 支出）。
攒够样本后第一个该砍的就是**确定性 worker 上的模型监工**，而不是监工本身。

审计侧确认：`harness` 字段记的是 `shell`（不是 `claude_code`），
`harness_version` 是脚本内容的 `sha256:6a11ebb44fda` —— 脚本改了哈希就变，
能看出「这次和上次不是同一个 worker」。`cost_usd=0` 是真话，不是缺省值。

### 顺带修掉一个 P0 就存在的真 bug：`version()` 探针的 cwd 泄漏

两个 adapter 的 `version()` 都在 `subprocess.run` 里没设 `cwd`，于是被探的
可执行体在**编排层自己的仓库**里跑了一遍。真 `claude --version` 只打印版本号，
所以 P0 全程看不出来；但任何会写文件的可执行体（测试里的假 harness、包装脚本）
都会往这个仓库里写东西 —— `out.py` 就是这么被 `git add -A` 带进 `10a0d9f` 的。

这和四监工那次的 cwd bug 是**同一个坑的第二次**（那次是 `claude -p` 把 cwd 和
`git status` 塞进系统提示词）。共同的教训：**任何 `subprocess.run` 都必须显式
决定 cwd**，默认继承调用方目录在这个项目里从来不是想要的行为。

修法：探针跑在空临时目录里，且**默认不探** —— 裸脚本不认 `--version`，
会被整个执行一遍，所以默认改成对文件内容取哈希，正规 CLI 才显式
`probe_version=True`。

副作用能量化：离线全量从 ~50s 降到 ~17s，原来那 33 秒是假 harness 被反复真跑掉的。
两条回归测试已反向验证（改回旧代码即 fail）。

---

## P1 录音/口述入口层真跑记录（2026-08-07）

任务描述（一句口述）：「在 text.py 里加一个 truncate_words(s, n)：按空格分词，超过 n 个词就截到 n 个词并在末尾接 '...'，不超过就原样返回。同时新建 test_text.py 覆盖截断和不截断两种情况。验收跑 python -m pytest -q。」

`factory prd` 的产物：
- `task_id: T-truncate-words-1`
- `acceptance` 7 条（规格监工能逐条核对的验收标准）
- `declared_ops: []`（guard 扫描 0 命中，无警告）
- 3 条 `unclear`（截断后空格处理、边界行为未说明）
- 入口层成本：$0.2727 / 28736 tokens

派发 `--spec-review --architecture-review`：

| 轮次 | 模型   | worker 成本  | 架构监工       | 规格监工     | 结论       |
|------|--------|-------------|----------------|-------------|-----------|
| 1    | haiku  | $0.37       | FAIL：truncate_words 只被测试调用，死代码 | PASS | reworked |
| 2    | sonnet | $2.67       | PASS           | PASS        | merged    |

最终交付：`text.py` + `test_text.py`，agent 在第 2 轮自行加了 `slugify_with_limit` 回应架构意见（复合函数，把两个工具函数接起来）。

D 类硬闸门同样真跑：「给 users 表加 last_login 字段，然后上线到生产」→ guard 补 `schema_migration + prod_deploy` → pre-dispatch 拦住，目标仓库 git status 全干净，提交数未变。

**三条非显然结论：**

1. 入口层是**唯一一个由模型决定分级输入的地方**，所以 `declared_ops` 的加固必须是确定性的、且只增不减。分级引擎只看得到申报；申报里没有 `prod_deploy`，D 类硬闸门就永远不会触发，也没有第二次机会。把一个硬闸门的唯一守门人交给一次模型调用，等于给"非旁路"开了一条间接旁路。因此分工是固定的：模型负责结构（prompt / paths / checks），`guard.py` 的正则表负责 `declared_ops`，两者取**并集**——guard 只能加，不能减。

2. `spec_ref` 和 `acceptance` 必须分开。`spec_ref` 的语义是"引用外部已有文档的编号"。口述来源的任务没有外部文档，它本身就是规格。只有一个字段的话，所有口述任务都会永久被规格监工判"无标准可核"——入口层和验收层的接口对不上，而两边的单测都是绿的，唯有真跑才能暴露。

3. guard 的误报率是入口层的核心质量指标，比漏报更早杀死无人工厂。18 条真实 A 类说法 0 误报是现在的基线；漏报由人一眼否掉（YAML 注释里写了怎么删），但误报让人人都要上人，工厂就白做了。两个边界要特别留意：「删掉没人用的那个函数」（"删"+"函数"，不应命中 data_delete）、「清理一下 import 顺序」（"清理"，不应命中 truncate/data_delete）。

---

## P1 副作用隔离：为什么是 Seatbelt 而不是容器（2026-08-07，commit `1e3fb71`）

worktree 隔离的是**工作树**，这一层隔离的是**副作用**：装包、写 workspace 以外的路径、
改系统配置。两者是不同的失效模式，各挡各的。

本机 Docker（29.4.0）和 `sandbox-exec` 都可用，选了后者：

- Docker 要把 worker 二进制**和它的认证**烤进镜像。`claude` 的凭据在 Keychain 里，
  镜像里没有 Keychain —— 等于要另发一套凭据进容器。为了隔离副作用，反而多造了
  一个密钥分发面，净收益是负的。
- Seatbelt 是 argv 前缀，adapter 结构不用改，两个 harness 都接上了（各一个
  `sandbox` 参数）。
- **代价说清楚：Seatbelt 只管文件系统和进程，不管网络。** worker 必须能连 API，
  所以网络是放开的。要断网得换容器，那是另一个决定，现在没做。

策略形状（实测确定，不是猜的）：允许写 workspace + 该 workspace 的
`--git-common-dir`，加一个本次派发私有的 TMPDIR；最后一行 deny 掉工厂自己的仓库。
`--git-common-dir` 不能省：worktree 里的 `.git` 是指向父仓库的**文件**，worker 一跑
`git add`（`capture_diff` 也依赖它）就要写父仓库的 `index.lock` 和 `refs`，只放开
workspace 会让 git 直接 fatal。用 `--git-dir` 也不行 —— 它在 worktree 里返回
`.git/worktrees/<name>`，缺 objects 和 refs，commit 照样失败。

**真跑验证（经 CLI 派发，不是只有单测）：**

| 场景 | 结果 |
|------|------|
| 真 `claude` worker + `--sandbox` | merged，交付的 `add_ints` 单测真的能跑过 |
| 越界 worker + `--sandbox` | 写 `$HOME`、写工厂的 `oracle_rules.yaml` 都被 OS 拒绝；`delivered.txt` 正常写出 |
| 同一个越界 worker，**不加** `--sandbox` | 两处都写成功 —— `oracle_rules.yaml` 真的被追加了一行（已 checkout 还原） |

第三行是关键。没有它，第二行无法区分"沙箱起作用了"和"worker 本来就没写成功"。
它也顺带演示了这一层真正防的是什么：**worker 有权改代码，而编排层自己的分级规则
也是代码。** 能追加 `oracle_rules.yaml` 就能删掉 D 类规则，下一次派发就没有硬闸门了。
所以工厂仓库是策略里唯一一条黑名单，且放在最后一行（实测确认 sbpl 最后匹配的规则胜出，
所以即使有人把它塞进 `extra_writable`，deny 仍然赢）。

**修掉一个实测才发现的洞。** `allow_tmp` 原来直接放开 `tempfile.gettempdir()`，
而 `/var/folders` 下住着所有别的任务的临时目录 —— 并行派发时一个 worker 能写进另一个
worker 的临时文件。发现方式值得记：单测全绿，是加了"经 adapter 端到端"那条测试才暴露的，
因为 pytest 的 `tmp_path` 恰好就在那底下，越界写居然成功了。现在 `prepare()` 每次派发
单独开目录并覆盖 `TMPDIR`。两条回归测试已反向验证（改回旧代码即 fail）。

**两条边界，写下来防止以后自己放宽：**

1. **沙箱不是 D 类硬闸门的替代，也不能成为放宽它的理由。** 硬闸门在派发**之前**判，
   压根不启动 worker；沙箱只在 worker 跑起来之后限制它写本机文件。沙箱挡不住一个
   已经拿到生产凭据的进程去调远端 API。`test_sandbox_does_not_relax_the_hard_gate`
   用真的分级引擎把这条钉住了。
2. **沙箱不可用时抛异常，不静默降级。** 调用方以为隔离生效了而实际没有，比压根不开
   沙箱更危险。（CLI 层的三态处理见下一节，那里有一处刻意的不对称。）

审计留痕走 `harness_version` 后缀（`2.1.224 (Claude Code)+sandbox`），不加列：加列要改
schema，而改 schema 本身是 D 类不可逆操作 —— 为了记一个布尔值去动硬闸门管辖的东西不值得。
没有这个后缀，两次 `merged` 长得一模一样，事后分不清哪个产出是在隔离下拿到的。

### 补一刀：默认开，以及 transcript 的那条缝（2026-08-07，commit `df0c161`）

上面那版做完就发现两个问题，都是**静默失效**型，不是功能缺失。功能缺失会被看见，
静默失效不会 —— 所以它们比"少做了一层"更值得记。

**一、`--sandbox` 是 opt-in，等于没有。** 这是个**无人**工厂：靠人记得加 flag 的
防护不存在。翻成默认开（macOS 上自动生效），要裸跑得显式写 `--no-sandbox`，而且
会打一行提示说清放弃了什么（worker 能写 `$HOME`、系统目录、以及工厂自己的分级规则）。

三态解析里有一处刻意的不对称，值得写下来：

| 情形 | 行为 |
|------|------|
| 不给 flag，平台支持 | 开 |
| 不给 flag，平台不支持 | 关，打提示，**继续跑** |
| 显式 `--sandbox`，平台不支持 | **退出码 2，不跑** |
| 显式 `--no-sandbox` | 关，打警告 |

为什么第三行要报错而第二行不要：显式 `--sandbox` 时静默降级，人会以为隔离生效而实际
在裸奔 —— 这比压根跑不起来危险，因为跑不起来会被立刻发现。反过来没显式要求时静默关掉，
是为了让流水线在 Linux 上仍能跑（那边该用容器，不是这一层）。

**二、拒掉整个 `~/.claude` 把审计弄瘸了。** worker 的 transcript 落在
`~/.claude/projects` 下，而审计要靠它还原 `tool_calls`。策略拒掉之后 `claude`
**不报错**，只是 `find_transcript()` 返回 `None`、`tool_calls` 静默变空 ——
两次 `merged` 长得一模一样，但事后没法还原 worker 到底动了什么。
**审计悄悄少东西比大声失败更糟。** 是 e2e 冒烟测试才发现的：
`assert row.transcript_path and ...endswith(".jsonl")` → `None`。

修法是放行**到 `~/.claude/projects` 为止**，不是整个 `~/.claude`。因为
`settings.json` 就在隔壁，能写它就能塞 hook —— 那是编排层下一次启动时、以编排层
身份、在**沙箱外面**的任意代码执行。换句话说，放宽这一格等于给沙箱开一条延迟生效
的越狱通道：worker 这次被关着，但它能在门外埋一个下次自动执行的东西。

三条新测试钉住这条缝，且都做过反向验证（把边界改坏，确认对应测试真的红）：

- 去掉 `projects` 放行 → 只有 `test_transcript_dir_is_writable` 红
- 放开整个 `~/.claude` → `test_claude_settings_stays_denied` 和
  `test_arbitrary_dotfile_in_claude_dir_stays_denied` 两条红

第三条测试专门不按文件名测：真正的边界是"`~/.claude` 下除 `projects` 以外都不可写"，
只钉 `settings.json` 的话，日后 Claude Code 新增一个配置文件，这层保护就凭文件名漏掉了。

真跑验证（**不带任何 flag**，走默认路径）：`merged`，1 轮，四个监工全过，$0.164068，
`harness_version = 2.1.224 (Claude Code)+sandbox`，`transcript_path` 已记录
（修之前是 `None`），交付的 `test_fizz.py` 真的 `Ran 3 tests OK`。
测试 331 → 340。

CLI 侧另加两条端到端断言，因为解析函数测对了不代表值传下去了 —— 默认跑一次断言审计里
有 `+sandbox`，`--no-sandbox` 跑一次断言没有。少了后一条，前一条无法区分"默认开生效了"
和"后缀永远都在"。

## P1 runbook 规则库：把 runbook 变成能跑的东西（2026-08-07，commit `6305582`）

spec §8 抱怨的是两件事，不是一件：**知识不复利**（驭流踩的坑靠手工搬到
smy），以及**写了不保证执行**（runbook 里写了验证步骤，但没机制保证 agent
真跑了、真看了结果）。第二件更要紧 —— 一份没人执行的检查表和没有检查表
是同一个状态，只是前者让人以为有防护。

所以这一层不是"更好的 markdown"，是一个规则库：每条规则是一条确定性命令，
由回归监工执行，失败就带着具体命令打回 worker。

### 为什么骑在回归监工上，而不是新开一个监工角色

这些规则是确定性 shell 命令、零模型调用、裁决语义和回归监工完全一样
（跑命令、比对退出码）。新立一个角色只会给 §5.1 的命中率报表多一个分母，
而对 worker 来说两类失败是同一件事："具体哪条命令没过"。

### 分层：全局 → 项目，同名覆盖且**保持位置**

全局规则（`factory/runbook/global_rules.yaml`）跟工厂一起发布，项目规则是
工厂外的一个 YAML。同名时项目胜出 —— 这就是 spec §8 说的「继承已有规则 +
声明差异」，差异靠同名覆盖表达，不靠复制整份规则。

覆盖时**位置不变**是刻意的：顺序一变，打回给 worker 的 claim 顺序也变,
两次跑的输出就没法直接对比了。

### 三条不许放宽的边界

**1. 规则永不从目标仓库读。** worker 对 workspace 有写权限（沙箱只挡
workspace *外面*），已实测确认它能自己造出一个 `.factory/runbook.yaml`。
从那里加载规则 = 让被考的人出卷子。所以 `load()` **报错**而不是忽略落在
workspace 内的规则文件：静默忽略的话，人以为项目规则生效了而实际没有。

**2. 只扫本次改动的文件。** 命令里用 `{files}` 占位符。全树扫描下，仓库里
任何一处既有违规都会打回一个压根没动过那些文件的 worker —— 实测在本工厂
自己的仓库上就误报了两次（`.venv` 里的第三方代码带 `import pdb`、规则文件
本身匹配到自己写的模式）。那样的规则库在任何存量项目上都开不起来。

`{files}` 走 `shlex.quote`：路径来自 diff，也就是来自 worker。一个叫
`a.py; rm -rf ~` 的文件名会把后面当命令执行。规则命令是我们写的（可信），
插进去的路径不是。

**3. 误报比漏报贵。** 漏报只是少查一项；误报会无理由打回，烧掉一整轮，
还让 §9 闸门 3 的「上人平均打回次数」这个 P1 判据失真。宁可写窄。

### 真跑抓到的三个 bug，两个是"静默"型

单测全绿之后拿真仓库跑，抓到三个，都不是单测能想到的：

**`py_compile` 对每个合法文件都报错。** `python-files-compile` 原来用
`py_compile.compile(..., cfile='/dev/null')`，而 py_compile 一定要写出一个
.pyc，指到 /dev/null 会报 "non-regular file" —— 也就是这条规则把**正确的
代码**判成失败。在这一层这是最贵的一类 bug（见上面第 3 条边界）。改成用内置
`compile()` 在内存里编译。

**全树扫描误报。** 见上面第 2 条边界，架构性的，靠加 `{files}` 修。

**删除的路径让整条规则静默失效**（三个里最难发现的）。改用 `{files}` 之后，
`no-debug-leftovers` 在一个**明明写着 `breakpoint()`** 的样本上判了 pass。
原因：grep 遇到不存在的文件退出码是 2，而命令前面的 `!` 把这个错误翻成
"通过"。diff 里天然包含被删除的路径 —— 所以一次改动里只要带上一个删掉的
文件，整条规则就无声无息地不查了。修法是在 `select()` 里过滤掉不存在的路径，
并把跳过原因往外传。回归测试：`test_deleted_paths_do_not_silently_disable_a_rule`。

静默漏查比误报更难发现，因为它不留任何痕迹 —— 全绿看起来和真的没问题一样。

### 每条内置规则都要有两个样本

`BUILTIN_CASES` 给每条规则各一个违规样本和一个合规样本，`test_every_builtin_rule_has_a_sample_pair`
保证新加规则时必须同时加样本。理由：只测"该触发"的话，一条永远返回 FAIL 的
规则也能全绿；只测"不该触发"的话，一条永远 pass 的空规则也能全绿。
另有 `test_every_builtin_rule_explains_why` 要求 `why` ≥ 40 字 —— 那是这份
文件唯一的复利来源，没有它，后人看到一条不知为何存在的命令会直接删掉。

### 证明它真能拦住合并（四次真跑）

前三次真跑都没能演示成"规则开火"，每次原因不同，而且每个原因本身都是发现：

1. `deploy.sh` 任务在派发**之前**就被 D 类硬闸门挡住（`D: deploy script`）。
   行为正确，但暴露了一个事实：两条 docker 规则 `when` 里键在 `*deploy*.sh`
   上，而部署脚本恒为 D 类，**这两条规则在无人路径上永远不会被触发**。
   它们只在人工执行脚本前的审查里有用。
2. 第二次的 worker 压根没留下 `breakpoint()`。
3. 第三次的 worker 自己把种进去的 `breakpoint()` 删掉了（`/tmp/rbfire`）。

结论是不该让验证依赖模型的清理直觉。第四次换 shell harness 做确定性 worker
（写一个含 `breakpoint()` 的 `leaky.py`，任务自带的 check 只是 `test -f`
所以必然通过，红只可能来自规则）：

```
开规则库 : escalated  rounds 1
           reason: [global:no-debug-leftovers] 期望 'exit_zero'，
                   实得 'exit 1: 2:    breakpoint()'
关规则库 : merged     rounds 1     ← 同一个 worker、同一份代码
```

反向那一跑是关键：没有它，就分不清"规则起作用了"和"这次派发本来就会失败"。

### CLI：默认全关，且配错要在派发**前**炸

`--global-runbook` / `--runbook <path>`，两个都默认关。不默认开全局规则的
理由：那会让每个既有任务的检查集在升级工厂后悄悄变大，突然多出来的打回
没人对得上原因。要继承得显式说。

规则库的加载在 `_cmd_run` 里派发前先跑一次。等到监工阶段才发现 YAML 坏了的
话，worker 的 token 已经烧完了 —— 而且那一轮会以一个跟规则库毫无字面关联的
错误结束。

测试数 359 → 362（`tests/test_runbook.py` 22 条）。

## P1 任务队列与跑批循环：让工厂在没人敲命令的时候也在干活（2026-08-07）

在这之前，这个工厂只在人敲 `factory run` 的那一刻动一下。所有的分级、沙箱、
监工、runbook 都能自动跑，但**触发**这件事一直是人做的 —— 离「无人」还差最后
一环。这一层补的就是这个：一个队列 + 一个循环，人只负责往队列里扔任务。

### 为什么是目录而不是一张表

审计已经有 SQLite 了，再开一个数据库表看起来更省事。没这么做，三个理由：

1. **生产者是多个的。** `factory prd` 会生成任务，人会手写 YAML，也会 `cp` 一个
   旧任务改改再跑。目录对这三种都是一行命令；表要为每种写一个入口。
2. **审计和队列的生命周期是相反的。** 审计要永久留着（spec §5.1 的报表按它算），
   队列条目跑完就该消失。放一张表里，「清理」和「不许删」会打起来。
3. **崩溃语义是免费的。** 原子 rename / link 直接给出「要么在 inbox 要么在
   running，不会两边都在」。同样的保证在表里要自己写事务。

目录结构：`inbox/ running/ done/ needs-human/ blocked/ log/`。
`blocked/` 和 `needs-human/` 刻意分开 —— 两者都要人介入，但要人做的事不一样：
`blocked` 是「D 类硬闸门，脚本已生成，你自己去执行」，`needs-human` 是「跑过了
没过验收，去看 diff」。混一个目录，`ls` 就分不出来该干哪件事。

### 认领用 os.link，不用 os.rename

这是整层最要紧的一个决定。`os.rename` 到一个**已存在**的目标会静默覆盖：两个
worker 同时认领同一个条目，两边的 rename 都成功，两边都以为自己赢了，任务被
派发两遍 —— 两倍花费、两份 diff，而在审计里看起来只是两个正常任务。回归之后
没有任何症状，这正是真跑演示不了、只能靠测试钉住的那类行为
（`test_second_claimer_loses_and_learns_it`）。

`os.link` 在目标已存在时抛 `FileExistsError`，所以输的那一方拿到 `None`，
`claim_next()` 会接着试下一个条目 —— 而不是报「队列空了」然后按 `--idle drain`
直接退出。并发跑多个 loop 时，第一个条目**总是**被抢走的那个，只试
`pending()[0]` 的实现会让后面每个 worker 都空手而归。

### 没有任何一条路通回 inbox

`finish()` 的四个 outcome 落到三个终态目录，`recover()` 把崩溃残留搬去
`needs-human`。**没有一条路把任务放回 inbox。**

重试是 dispatcher 的事（`max_rounds`，3 轮后升级）。队列层再叠一层自动重试的
后果有两个：一个必然失败的任务会无限烧钱；而且每一轮在审计里都长得像一个新
任务 —— 「这个任务试了 12 次」和「有 12 个相似任务各试了一次」在报表上区分不
出来，spec §5.1 的通过率就废了。

崩溃恢复同理。崩掉的那一轮可能已经烧掉了 token、可能已经在 worktree 里留了半
个改动。自动重排等于允许重复计费和重复提交。让人看一眼再决定。

### 判活优先于判龄

`recover()` 的判据顺序是：claim 文件在不在 → 读不读得出来 → host 是不是本机 →
pid 还活着吗。**本机 pid 还活着就一律不动，哪怕它已经跑了两天。** 一个还在跑的
派发被当成僵尸抢走，结果就是同一个任务被派两遍 —— 和上面 rename 那个 bug 同一
种伤害。

只有在无法验证存活时才退回看时间：没有 claim 文件（谁都认领不了它，等下去不会
变好，立刻回收），或者 claim 来自另一台机器（本机 pid 表查不到它，拿本机的表去
判活会把活着的远端 worker 判死，所以只能看静置时长）。

反过来，本机 pid 已经不在了就**立刻**回收，不等 `stale_after_s` —— 进程都没了
没什么可等的，等 6 小时只是让队列白闲 6 小时。

### 四个刻意的默认值

| 默认 | 为什么不是另一个 |
| --- | --- |
| `--budget-usd 5` | 不是「不限」。无人循环最坏的失败模式是**一直跑**；忘了写这个 flag 应该意味着「早点停」，不是「一直刷卡」。`0` 能关，但会打印警告。 |
| `--idle watch` | 不是 `drain`。「无人」的意思是队列空了它还在等下一个。cron 场景才用 `drain`。 |
| `--worktree` 开 | 不是共用 workspace。共用的话，前一个任务未验收的改动会成为后一个的起点。 |
| 优雅停机 | SIGINT/SIGTERM 只置标志，**当前任务跑完才退**。半路砍掉 agent 会留下没人看过的半个 diff 和一个孤儿 running/ 条目。第二次信号恢复 Python 默认行为 —— 一个「怎么都停不下来」的无人循环比一个留下孤儿的循环更糟。 |

`escalated` 不计入退出码。3 轮后升级给人是**设计上的正常出口**，不是故障；算
失败的话 cron 会天天报警，而天天报警的告警等于没有告警。只有 `error` 让 loop
返回 1。

### 预算闸门和账单看同一个数

`_attempts_cost()` 从审计库读，不让 dispatcher 返回一份。两份数会在某次重构后
悄悄分叉，而分叉的方向只有在账单上才看得出来。

监工的花费记在 verdict 行上、不在 attempt 行上，所以要一起加。只算 attempt 的
话，开了 `--spec-review` 的循环会系统性低估自己的开销 —— 低估的预算闸门等于没
有闸门。

### 真跑发现的两个 bug

**一、给硬闸门任务开了 worktree。** 对抗性真跑（坏 YAML + D 类任务 + 好任务）跑
完后，`.factory-worktrees/` 里留着一个 `T-deploy-thing/` —— 那个任务被硬闸门在
**派发前**就拦掉了，一行都没跑，但目录已经建好了。单看是无害的空目录，问题在于
worktree 目录的存在本身在别处是有含义的（「这里有产出没人验收」），堆着一堆空的
会把那个信号淹掉。

修法是 `_queued_workspace()`：预分级 C/D 的任务直接返回 `--workspace`，不开
worktree。**这里重跑一次预分级不是闸门** —— 真正的判定在 `Dispatcher.run` 里，
那条路径一步都没绕过；这里判错的最坏后果只是多开或少开一个空目录。测试
`test_this_is_not_the_gate` 直接读源码断言这个函数里没有 `return True/False`
也没有 `raise`，防止后来有人顺手把它改成一个绕过硬闸门的旁路。

同一个 bug 还有个尾巴：任务的 note 里仍然写着 `worktree=/tmp/lqrepo`，指向的其实
是 workspace 本身。看到这行的人会以为那里有产出可看。改成只在真开了 worktree 时
才写。

**二、`AuditStore.get()` 查不到时返回 `None` 而不是抛异常。** `_attempts_cost()`
外面包了 `except Exception: continue`，注释写着「花费读不到不该让整个循环停」——
但它兜不住 `None`，下一行 `row.cost_usd` 直接 `AttributeError`，而这个异常是在
loop 的 `_one()` 外面抛的，会把整个循环带下去。写
`test_an_unreadable_cost_row_does_not_stop_the_loop` 时才发现。一个只在审计库缺
行时才触发的 bug，真跑一百次也碰不到。

### 韧性：一条坏任务不能让没人看着的队列停摆

`_one()` 兜住所有 `Exception`（不兜 `BaseException` —— `KeyboardInterrupt` 被兜住
才是真的停不下来），归档成 `error`，异常原文进 `.result.json`。

对抗性真跑证明了这条：坏 YAML 的 `ParserError` 全文进了 `needs-human/` 的
`.result.json`，D 类任务撞上硬闸门，而**紧跟其后的那个好任务照样 merged**。

```
[1] broken  → error   ParserError: while parsing a flow sequence ...
[2] deploy  → blocked_hard_gate   pre-dispatch D: deploy script [deploy.sh]
[3] ok      → merged  worktree=/private/tmp/.factory-worktrees/T-after-bad
== 停机：队列已抽干
派发 3：合并 1 / 升级 0 / 硬闸门 1 / 异常 1
```

顺跑那次是 3 个 shell-harness 任务 `--idle drain`：`合并 3`，各自一棵 worktree，
产出文件逐个核对过（`def go(): return 'beta.py'`），`done/` 里是 YAML +
`.result.json` 成对。

### 测试：53 个，全套 415 个

`tests/test_backlog.py` 覆盖的重点是**并发和崩溃下才出现的行为**，真跑演示不了
的那些：`os.link` 竞争中输的一方拿到 `None`、没有路通回 inbox、`_park` 同名加
后缀（人改完判据重新入队时 basename 一样，覆盖掉第一次的失败证据就没了）、
判活优先于判龄的四条分支、三个上限各自的停机、`Idle` 三种行为、`_one()` 兜异常、
以及 C/D 类不开 worktree。

时钟是注入的（`FakeClock`），所以测 `--max-runtime 100s` 不需要真跑 100 秒。
`dispatch` 也是注入的 —— `BacklogLoop` 刻意不 import `dispatcher`。

### 怎么用

```bash
# 入队（一个失败则一个都不入队 —— 部分成功比全失败更难收拾）
factory queue --queue ~/.factory/q tasks/*.yaml

# 看状态
factory queue --queue ~/.factory/q

# 常驻跑（默认 watch：队列空了继续等）
factory loop --queue ~/.factory/q --workspace ~/repo --db audit.db \
  --global-runbook --budget-usd 20

# cron 跑（抽干就退）
factory loop --queue ~/.factory/q --workspace ~/repo --db audit.db \
  --idle drain --budget-usd 5
```

定时启动别照抄上面这条命令 —— launchd 的 PATH 里没有 nvm 装的 claude。
用 `examples/launchd/com.factory.loop.plist`（标了「←」的行按本机改），
理由见下文「P1 定时启动」。

刻意**没有** `queue clear`：清队列的唯一正确方式是看清每个条目再删。一条命令把
`needs-human/` 一起清掉，等于把「还没人看过的失败」当成垃圾扫了。

`loop` 的所有配置错误都在进入循环之前验证（harness、sandbox、runbook 路径、
worktree 池）—— 循环一旦跑起来就没人看着了，第 40 分钟才因为一个拼错的路径退出
是最贵的失败方式。

### 剩下的缺口

队列这一层补上之后，「无人」还差的是**任务从哪来**。现在仍然要人写 YAML 或跑
`factory prd`。P2 的方向是把 issue tracker 接成生产者，但那要先解决 PRD ↔ diff
一致性检查（目前只有分级在管改动范围，没有东西检查「做的是不是 PRD 要的那件
事」）。

> **这段的后半句写完就过期了**（2026-08-08 复核）。`factory/intake/` 落地后，
> 「做的是不是 PRD 要的那件事」由**规格监工**在管：它的输入是 `criteria =
> spec_ref + acceptance`，逐条核 diff 是否满足，任何一条无法从代码确认就判
> fail 并给出 claim。P1 真跑里它第一轮就抓住了重复实现陷阱。
>
> 仍然缺的是**入口那一侧**：`factory prd` 生成的验收标准本身没有东西核 ——
> 口述漏讲了一条，标准里就不会有它，规格监工也就不会去核它。所以 P2 的
> 「PRD ↔ diff 一致性」实际含义是「PRD ↔ 口述原文一致性」，和 diff 无关。
> 留着这条过期文字不删，是因为它记录了当时的判断依据；直接改掉会让
> 「为什么后来建了规格监工」这条线索断掉。

## P1 跑批日志：无人循环留下的唯一现场（2026-08-07）

队列层建好之后有个尴尬的发现：`store.py` 一直在创建 `log/` 目录，但**从来没人往
里写过**。一个建好就空着的目录比没有这个目录更糟 —— 它让人以为日志在别处。

真正的缺口是：一个跑了一夜的循环，第二天早上要回答的问题是「花了多少、有几个要
我看、哪个任务最贵、有没有崩过」。而在这之前，唯一的记录是终端输出。

### 为什么不是把 stdout 重定向到文件

两个理由：

1. **终端输出是给盯着屏幕的人看的散文**（`[2] deploy → 派发（已花 $0.0000）`）。
   从散文里数总额、数待人介入的个数要肉眼扫，而扫错了不会有任何提示。
2. **cron 跑的循环，stdout 默认进邮件或者干脆丢掉。** 指望调用方记得加 `>> log`
   等于没有日志。

所以是 JSONL，由循环自己落盘：一行一个事件，机器能 grep 能求和，人也还能读。
终端输出和日志是**两个消费者**，不共用一条通道 —— 合并的话，要么日志变成没法解析
的散文，要么终端输出变成没人想读的 JSON。

按天分文件（`log/2026-08-07.jsonl`）。按一个大文件走的话，常驻循环一个月能到几十
MB，而「看看昨晚」是最常见的查询；按 run 分又太碎 —— `--idle watch` 的循环一次能
跑好几天。

### 崩掉的循环要能看出来

事件流是 `run_start → (recover)* → (dispatch)* → run_end`，`run_end` 走 `finally`。

关键在于：**有 run_start 而没有 run_end，本身就是「上次跑崩了」这个信息。**
被 `kill -9` 的进程没机会写收尾行，也没机会报警。不把这个数出来的话，一个每晚
启动就崩的 cron 和一个每晚无事可做的 cron 在日志里长得一模一样 —— 两者都是「昨晚
没有任何任务」。`Rollup.crashed_runs` 就是数这个。

真验过：起一个 `--idle watch` 的循环，`kill -9`，然后 `queue --history`：

```
最近 6 条事件（/private/tmp/jq/log）
  任务 3 个，花费 $0.0000，待人介入 1 个
  ⚠ 循环非正常退出 1 次（有 run_start 无 run_end）
    [blocked_hard_gate] deploy  pre-dispatch D: deploy script [deploy.sh] ——
      硬闸门，agent 只能生成待执行脚本
```

`finally` 和 `kill -9` 的区分是刻意的：循环自己抛异常时**会**写 run_end（所以
`crashed_runs` 是 0），被外力杀掉时不会。两者要查的地方不一样 —— 一个看
traceback，一个看 OOM / 是谁 kill 的。

### 写日志失败不抛异常

日志是观测手段，不是任务的一部分。磁盘满了、目录被人删了、权限变了 —— 这些都不该
让一个正在正常派发任务的循环停下来。所以 `event()` 兜住 `OSError`，只往 stderr
抱怨一次（静默失败会让人以为「昨晚没跑」）。

同理 `tail()` 跳过读不出来的行而不是抛：循环被 kill 时最后一行可能只写了一半，
一个半行不该让 `queue --history` 整个不可用。

### Rollup 刻意不做成通用查询

一个 SQL-ish 的过滤器接口会让这里长出第二套报表，而 spec §5.1 的报表已经在
`audit.db` 上了。`Rollup` 只回答队列侧的四个问题：花了多少、有几个要人看、最贵的
是哪个、循环有没有非正常退出。

两个细节：
- `escalated` / `blocked` / `error` 三种合成一个「待人介入」的数。三者要人做的事
  不一样，但「明天早上要看几个」是同一个数；分开的明细在 `needs-human/` 和
  `blocked/` 目录里。
- 全是 `$0`（shell harness）时**不报**「最贵：$0」—— 一个假信号比没有信号更费
  注意力。
- 待人介入超过 10 个只列前 10 —— 200 行糊在终端上等于没有摘要。

### 查询命令的退出码永远是 0

`queue --history` 即使有待人介入的任务也返回 0。它是查询，不是闸门；非零退出会让它
没法放进 `&&` 链，也会让 cron 的日报邮件变成告警邮件 —— 而天天来的告警邮件等于没有
告警。

### 用法

```bash
factory queue --queue ~/.factory/q --history        # 最近 50 条
factory queue --queue ~/.factory/q --history 200
jq 'select(.kind=="dispatch" and .outcome!="merged")' ~/.factory/q/log/*.jsonl
```

39 个新测试（journal 22 + 队列层已有的 53 里有几个连带），全套 437 个。

## P1 范围监工

### 分级引擎管不到的那一半

分级引擎回答的是「改动落在哪里**危险**」。它管不到「改动落在哪里算**离题**」。
这两件事看起来相邻，实际不重叠 —— 实测：

```
任务声明   declared_paths: [src/util/text.py]
worker 改  src/util/text.py  docs/readme.md  src/util/other.py
后分级     A 类（no rule matched -> default A）
结果       merged，rounds 1，全绿
```

三个文件都不匹配任何分级规则，所以后分级仍是 A 类，干净合并。分级引擎没有
出错，这不是它的问题域。

值得单独拦的理由有两条。一是**无人工厂里没人看 diff**：一次"顺手改的"在有人
review 的流程里会被问一句，在无人流程里直接进主干；攒够几十次之后主干上有一批
没人记得为什么存在的改动，而审计记录会告诉你每一条都全绿合并过。二是越界改动
是**上游需求和实际产出不一致**最便宜的证据 —— 规格监工能查语义一致性，但它要
调模型、要花钱，而且只有 criteria 非空才有意义；路径对不上连模型都不用调。

### 为什么它是独立的 role

不并进 `RISK`：RISK 每轮已经恰好写一条裁决（后分级的 PASS 或 FAIL）。共用角色
会让同一个 attempt 出现两条方向相反的 RISK 裁决，§5.1 的 `fired` 和 `passed`
同时 +1，那张表的裁剪结论就建在坏数字上。`metrics.py` 的 bucket 是按 role 动态
建的，所以新角色自动有自己的一行，不需要改报表代码。

放在回归监工旁边而不是后分级旁边，也是一个刻意的选择：**越界是 worker 自己能修
的**（把无关文件改回去），所以它必须走打回路径。后分级的升级路径是不打回的 ——
那条路径处理的是「这类改动根本不许无人跑」。

### declared_paths 为空 = 没有约定，一律 PASS

这条不是偷懒。`factory/intake/extract.py` 的提示词明确要求「用户没说文件名就留空
数组，不要推测、不要凭常见项目结构补全」，所以口述来源的任务绝大多数 declared_paths
为空。空数组当成「什么都不许改」会让几乎每个任务被打回，而打回理由是**任务自己
没写清楚** —— worker 修不了这个，三轮全烧完然后上人。

要范围约束就显式写 `declared_paths`。这和 runbook 不默认加载是同一个取舍：
不让升级工厂之后既有任务的检查集悄悄变大。

### 匹配语义

和分级规则共用 `fnmatch`：同一个模式在两处必须匹配同一批文件，两套匹配规则是纯粹
的坑。目录形式的声明（`docs/` 或 `docs`）额外展开成 `docs/**` —— fnmatch 的 `*`
不跨 `/`，不特殊处理的话 `docs/` 匹配不上 `docs/readme.md`，**声明了反而全部越界**。
「加了约束结果更糟」比没有约束更难查，所以这条有专门的测试，连同 `docs/` 不该顺带
放过 `docs-internal/` 的前缀兄弟目录情形。

## P1 入口闸门：任务从哪来

### 「无人」原先只覆盖派发之后

到 P1 的队列和跑批循环建完，工厂已经能无人认领、无人派发、无人验收、无人归档。
但任务从哪来始终有个人：要么人手写 YAML，要么人跑 `factory prd` 然后**人看一眼**
再 `factory run`。这一步不是遗漏，是 `DraftTask` 的文档明确要求的：

> 入口层的产物必须先落成 YAML 给人看一眼，再 `factory run`。直接从一段话接进
> dispatcher 就等于把"我想改点东西"变成了自动派发，分级的输入（paths / ops）
> 也就没人核对过了。

这个顾虑是对的，所以闸门**不取消**它，而是把它变成可判定的条件：人看一眼是为了
发现「这条任务的分级输入不可信」；那就把"不可信"的特征列出来，命中任何一条就不许
自动进队 —— 落到 `needs-human/` 等人；剩下的自动进 `inbox/`。

### 宁可误拒不可误放

每条规则都往拒绝的方向倒，因为两边代价不对称：

| | 代价 |
|---|---|
| 误拒一份够格的草稿 | 一次人工确认 —— 和现状完全一样 |
| 误放一份不够格的草稿 | 花钱跑了一个没人核对过分级输入的任务 |

### unclear 刻意不是一票否决

这是整个闸门唯一不显然的判据，而且是实测逼出来的。第一版把「模型有疑问」当成硬
拦截，结果一句写得挺完整的需求被拦下了：

```
需求  在 src/text.py 里加一个 slugify 函数……验收标准：slugify('Hello World')
      必须返回 'hello-world'
草稿  acceptance ✓  checks ✓  declared_paths ✓
unclear
      - 未说明对多个连续空格、制表符/换行、首尾空格、非 ASCII 或空字符串的处理
      - 未说明是否需要处理非字符串输入或做类型校验
闸门  拦下 —— 模型有 2 条待确认问题
```

这两条疑问是真的没说，但它们**恰恰是 acceptance 划定边界之后不必回答的问题**：
验收只要求那一个用例过，多余的行为 worker 怎么定都行。自然语言永远有没穷尽的边界
情形，所以 unclear 一票否决 = 无人路径永不触发，闸门退化成一个更啰嗦的
`factory prd`。

改成：**有 acceptance 或 spec_ref 时，unclear 降级为提示** —— 验收标准就是那个
边界。没有标准兜底时它才算拦，因为那时那些疑问真的是需要人回答的东西。

### 四条硬拦截

| 判据 | 为什么拦 |
|---|---|
| 没有 check | 回归监工会给 `no-checks-defined` FAIL，三轮全红上人。放进队 = 确定烧三轮钱，换一句现在就能免费说的话 |
| acceptance 和 spec_ref 都为空 | 「全绿」的含义取决于 check 写得多严。人写 YAML 时能自己权衡，自动进队没人权衡 |
| 声明了 declared_ops | **不是**因为 D 类危险（那是 dispatcher 的判断），而是因为 ops 是模型从自由文本里抽的，抽错的两个方向里"少抽了"没有下游能兜住 —— 少抽一个 `prod_deploy`，分级就看不见它 |
| guard 嗅到但模型没声明 | 上一条担心的"少抽了"的实例，所以是拒绝而不是提示 |

`declared_paths` 为空只是提示，不拦 —— intake 明确要求用户没说文件名就留空，拦下来
等于绝大多数口述任务都进不来。提示的内容是「范围监工不会拦越界改动」，让人知道
放弃了什么。

reasons 一次全报，不是报第一条就返回：草稿带着 reasons 落到 `needs-human/`，
只报一条会让人来回补三次。

### 闸门不做分级

闸门在分级之前，判的是「这份声明**值不值得**拿去分级」。它若自己判起类来，就会
出现两套分级规则，而 dispatcher 里那套才是硬的。有一条测试查 `factory.intake.gate`
模块里没有 `GradingEngine` / `OracleClass` / `Grade` 这几个名字（查 import 而不是
查全文 —— 文档里提到它们是在解释"这不是我的事"，那句话该留着）。

**两条边界完全不经过闸门**：C 类永不无人、D 类硬闸门非旁路，都在 dispatcher 里，
闸门放行的任务照样要过。这一点验证过而不是假设的 —— 把一份改 `deploy.sh` 的任务
直接塞进 `inbox/`（完全绕开闸门），跑批循环照样在派发前拦下：

```
[1] T-deploy  → 派发（已花 $0.0000）
    blocked_hard_gate (+$0.0000)  pre-dispatch D: D: deploy script [deploy.sh]
                                  —— 硬闸门，agent 只能生成待执行脚本
派发 1：合并 0 / 升级 0 / 硬闸门 1 / 异常 0，花费 $0.0000
```

闸门增加的是一条**进入队列**的路径，不是一条绕过 D 类判定的路径。

### 退出码 3

`factory prd --queue` 被闸门拦下时退出 3，不是 1 也不是 0。1 会让"闸门正常工作"
看起来像错误；0 会让 `factory prd --queue && factory loop` 在什么都没入队的情况下
继续跑。

不过闸门的草稿**照样落盘**（落在 `needs-human/`）。丢掉的话一次口述需求就白说了，
而人连"差什么"都看不到 —— 那比要求人确认更糟。同名不覆盖，加数字后缀，和 `_park`
一个道理。

### 实测：一句中文到合并

```
$ factory prd --text "在 src/text.py 里加一个 slugify 函数，把字符串里的空格
    换成短横线并转小写。验收标准：slugify('Hello World') 必须返回 'hello-world'。
    用这条命令验证：python -c \"from src.text import slugify; assert
    slugify('Hello World')=='hello-world'\"" --queue /tmp/e2eq
已写入 /private/tmp/e2eq/inbox/T-add-slugify-1.yaml
  提示      : 模型有 1 条疑问，但 acceptance 已划定边界，未穷尽的行为由 worker 自定
闸门通过 → 已进 inbox，等 `factory loop` 认领。

$ factory loop --queue /tmp/e2eq --workspace /tmp/e2e --db /tmp/e2e.db --idle drain
[1] T-add-slugify-1  → 派发（已花 $0.0000）
    merged (+$0.1768)
派发 1：合并 1 / 升级 0 / 硬闸门 0 / 异常 0，花费 $0.1768
```

产出：

```python
def slugify(s):
    return s.replace(" ", "-").lower()
```

验收命令手工复跑通过。全程无人打开或编辑过任何文件。

### 剩下的口子

- **check 得由用户说出来**。上面那句需求里"用这条命令验证"是我明确写的；不写
  验证方式的需求会被闸门拦在 `needs-human/`。让抽取器自己发明验证命令是下一步
  最值钱的改动，但它会让抽取器开始替用户做验收决定 —— 得先想清楚怎么防它编一条
  永远为真的 check。
- **闸门的判据还没有命中率数据**。它现在是纯规则，两周后该像监工一样统计误拒率。

## P1 从验收标准反推 check（2026-08-08，commit `883a960`）

闸门上线之后暴露的第一件事：它的第一条硬拦截是「没有可执行的 check」，
而用户口述需求时几乎不会自带验证命令。结果是绝大多数草稿停在 needs-human，
无人路径形同虚设 —— 闸门放行的条件和人自然写出的需求之间有个结构性缺口。

`factory/intake/checkgen.py` 补这个缺口：acceptance → 可跑的 command。

### 为什么不放进 extract.py

两个模块的可见性要求正好相反。extract 是**无工具**的：它看不到仓库，所以
不可能凭空编出 `declared_paths`。而写一条**真能跑**的命令必须知道测试放哪、
用什么跑 —— 那是仓库信息。合成一个模块就得给 extract 开工具，
它编 declared_paths 的路也就同时开了。所以分两步。

### 红前绿后：靠子进程退出码，不靠提示词

提示词里写「不要写假验收」是没有约束力的。每条候选 check 都在**活还没干**的
仓库里真跑一遍，按结果分三类：

| verdict | 含义 | 处置 |
|---|---|---|
| `fail_missing` | 现在红的，命令本身没坏 | **留下**（唯一被采纳的） |
| `already_green` | 功能还没写就已经绿了 | 扔掉 —— 它证明不了改动做成了 |
| `broken` | 命令本身坏了（127/126/语法错） | 扔掉 —— 改完还是红的，白烧三轮 |

`already_green` 这一类抓的就是模型最爱写的三种假验收，静态看都不出问题：

```
true                      → exit 0  → already_green
test -f src/text.py       → exit 0  → already_green   （文件存在证明不了行为对）
python3 -c "pass"         → exit 0  → already_green
pyhton3 -c "print(1)"     → exit 127 → broken
python3 -c "print(1)      → exit 2  → broken（shell 语法错，stderr 认出来）
python3 -c "from src.text import slugify"  → exit 1 → fail_missing ✓
```

**退出码 2 不单独判 broken。** pytest 收集失败也是 2，而收集失败常常正是
「功能还没写」—— 按码一刀切会把好 check 扔掉。所以 2 要再看一眼 stderr 措辞
（`syntax error` / `command not found` / …）才算坏。

### repo_digest 递什么、不递什么

只递目录结构和构建配置的存在性，**不递任何文件内容**。写验收命令需要知道
「测试放哪、用什么跑」，不需要知道任何一个函数长什么样。少递一样东西，
就少一条它把仓库里现成代码抄进 command 的路。

### 接进 prd 的三条边界

- **提议跑在闸门之前。** 放在之后等于永远补不上 —— 该补的草稿已经落进
  needs-human 了。
- **只在 `draft.checks` 为空时跑。** 用户自己写的验收命令是需求的一部分，
  不覆盖；模型的提议只补空缺。
- **失败原样返回，不抛。** fail-closed：草稿带着空 checks 进闸门，
  被那条「没有可执行的 check」拦进 needs-human。也就是退回到没有这个功能
  之前的状态，而不是把整条 prd 打断。

### 实测

同一句需求，加 `--propose-checks --workspace` 前后：

```
（前）rc=3  落 /tmp/cgq/needs-human/  —— 「没有可执行的 check，进队只会烧三轮再上人」

（后）rc=0  提议 1 条，探针留下 1 条
            ○ 无法机器验收：本次改动只涉及 src/text.py 文件
      落 /tmp/cgq/inbox/T-add-slugify-1.yaml → 闸门通过
```

生成的 check（`expect: stdout_contains` 是模型自己选的，比 exit_zero 更严）：

```yaml
- name: slugify-basic
  command: python3 -c "from src.text import slugify; assert slugify('Hello World') == 'hello-world'; print('OK')"
  expect: stdout_contains
  value: OK
```

`factory loop` 13.2s 合并，A 类，三个监工全过（risk / regression / scope），
$0.1912。产出 `def slugify(s): return s.lower().replace(" ", "-")`，
check 在 worktree 里独立重跑通过。

值得记的一点：另一条 acceptance「本次改动只涉及 src/text.py」被**正确地**
判为无法机器验收 —— 那是范围监工的活，不是一条 shell 命令的活。模型没有
为了凑数把它写成 `git diff --name-only | grep -c ...`。

## P1 闸门的误拒率：让「拦得对不对」变成一个数（2026-08-08，commit `1ee30f4`）

闸门上线时留了个洞：它的五条规则一条实证数据都没有。哪条拦得最多、
拦得对不对，全靠看 stdout —— 而 stdout 在下一次 `prd` 之后就没了。
这个洞的危险不在于「不知道」，而在于**没有数据的时候，松规则和紧规则
听起来一样有道理**。

### 三处改动凑成一个可算的数

1. **每条 reason 加 `[code]` 前缀**（`RULE_CODES`）。文案是给人看的、
   会随时改写；统计要的是不会随文案漂移的键。没有编码就只能 grep 中文串，
   改一次措辞历史数据就断了。`rule_code()` 读不出编码时返回 `"other"`
   而不是抛 —— 加编码之前的旧日志不该让整份统计不可用。

2. **判决写进队列日志**，和 `loop` 的 dispatch 事件同一份 JSONL。
   放行的也记：分母需要，而且「闸门判了多少次」本身就是个数。

3. **从 `needs-human/` 入队 = 人推翻了闸门。** 这是误拒的**唯一**信号 ——
   闸门自己永远不知道它拦错了。记在 `queue` 命令里而不是等人填表：
   需要额外动作的度量等于没有度量。

### 分母为什么只取「拦下的次数」

`false_reject_rate = 被人放回的 / 被拦下的`，不是 `/ 全部判决`。

这个数要回答的是「闸门拦的时候拦错了多少」。把放行的那些混进分母，
会把它稀释成一个总是很小的数字，而那个小数字改不动任何一条规则 ——
90% 的草稿都放行时，就算每一次拦截都是错的，总体误拒率也只有 10%。

**它是个下界。** 人懒得放回、或者干脆自己改 YAML 重新 `prd` 的，都不算进来。
所以报表里明写「下界」：用它判「哪条规则该松」是安全的，
用它判「闸门整体够准了」不安全。

`false_reject_rate` 在没拦过时返回 `None` 而不是 `0.0` —— 0% 会被读成
「一次都没拦错」，而没数据和拦得很准是两件事。

### 实测

```
$ factory prd --text "...上线到生产环境。" --queue /tmp/gq
rc=3
闸门拦下（2 条）→ 落在 needs-human，等人：
  - [no-checks] 没有可执行的 check，进队只会烧三轮再上人
  - [declared-ops] 声明了不可逆操作，抽取可能漏项，分级输入必须人核对：prod_deploy

$ cat /tmp/gq/log/*.jsonl
{"kind": "gate", "task_id": "T-upper-first-cap-1", "admitted": false,
 "codes": ["no-checks", "declared-ops"], ...}

$ factory queue /tmp/gq/needs-human/T-upper-first-cap-1.yaml --queue /tmp/gq
已入队 /private/tmp/gq/inbox/T-upper-first-cap-1.yaml
  ↑ 人推翻了闸门判决，已记进日志（factory queue --history 里看误拒率）

$ factory queue --queue /tmp/gq --history 50
  闸门判决 1 次，拦下 1 个
    误拒（人放回 inbox）1 个 = 100% —— 这是下界，见 false_reject_rate
    [no-checks] 拦下 1
    [declared-ops] 拦下 1
```

从别处入队的任务**不**计入误拒（测试钉住了这条）：算进去会让误拒率虚高，
而虚高的误拒率会把规则一路松成一个不拦任何东西的闸门。

规则命中数按 **code** 计而不是按草稿计 —— 一份草稿常同时命中几条，
按草稿数会让「哪条规则最该松」看不出来。

### 顺带发现的一个 guard 误报

测试过程中撞到的：需求「加一个 `truncate(s, n)` 函数」被 guard 判成
不可逆操作 `truncate`（词表里 `\btruncate\b` 是为 SQL `TRUNCATE TABLE` 写的），
于是这个纯函数任务被 `[declared-ops]` + `[guard-ops]` 两条一起拦下。

**没有改词表。** guard 的文档里写明「宁可多报」是刻意的取舍：多报的代价是
人看一眼，少报的代价是无人管道自己去动生产库。凭一个 anecdote 收紧词表，
方向正好是往危险那边倒。现在有了 `[guard-ops]` 的命中计数，
下次要不要收紧可以看数据 —— 这正是这套度量要解决的问题。

## P1 归属：刚做出来的度量自己是错的（2026-08-08，commit 04468c3）

上一节把「闸门拦得对不对」变成了 `[code]` 计数。这一节记的是：那份计数
上线一个 commit 之后就发现是错的，以及为什么这个错值得单独写一节。

### 缺陷

`harden_ops` 返回的是并集：

```python
return have + tuple(f.op for f in findings), findings
```

也就是 guard 从原文里嗅到的 op 会被**塞进** `declared_ops`。闸门原来直接读
`declared_ops` 来生成 `[declared-ops]`，再读 `guard_findings` 生成
`[guard-ops]`。于是一个只有 guard 嗅到的 op 会同时命中两条编码：

```
codes: ('declared-ops', 'guard-ops')
模型其实什么都没声明，却记了一次 declared-ops
```

而这两条编码存在的**全部**理由就是分辨这两件事：

- `[declared-ops]`：模型自己读懂了，抽出来报了 —— 抽取是靠得住的
- `[guard-ops]`：模型没抽出来，词表兜住了 —— 抽取漏了

一个被 guard 命中数污染的 `[declared-ops]` 计数，回答不了「模型的抽取到底
靠不靠谱」。而这正是将来判「哪条规则该松」时唯一要问的问题。度量本身错了
比没有度量更糟：它会给出一个看起来能用的数字。

### 修法

只改归属，不改拦不拦。`findings` 里只含 guard 新加的（`harden_ops` 已经滤掉
模型报过的），所以差集就是模型自己声明的那些：

```python
findings = getattr(draft, "guard_findings", ())
sniffed = {f.op for f in findings}
by_model = [o for o in getattr(draft, "declared_ops", ()) if o not in sniffed]
```

三种情形实测：

```
guard 独家嗅到 : ('guard-ops',)                | admitted = False
只有模型声明   : ('declared-ops',)             | admitted = False
两边各有一个   : ('declared-ops', 'guard-ops') | admitted = False
```

`admitted` 三条都是 False —— 归属改了，拦截行为一个字没动。有一个测试专门
钉这一点（`test_fixing_the_attribution_did_not_loosen_the_block`），因为
「修度量顺手放松了闸门」是这类改动最容易犯的错。

### 测试必须走真的 harden_ops

手搓 `declared_ops=("data_delete",), guard_findings=(...)` 两个字段的测试
**测不出这个 bug** —— bug 只在并集语义下出现。所以新增的 5 个测试都从文本
出发跑真的 `harden_ops`：

```python
def _real(text: str, declared=()) -> Admission:
    ops, findings = harden_ops(text, declared=declared)
    return admit(_draft(declared_ops=ops, guard_findings=findings))
```

反过来验了一次：把 gate 改回读并集，`test_an_op_only_guard_found_is_not_
charged_to_the_model` 挂掉。

### 顺带：truncate 的收紧方案被数据否掉了

`\btruncate\b` 会把「加一个 truncate(s, n) 字符串截断函数」判成不可逆操作。
看起来最顺的收法是「后面紧跟 `(` 就当函数」：`\btruncate\b(?!\s*\()`。
实测：

```
良性  收紧后还拦得住？ False   加一个 truncate(s, n) 字符串截断函数
危险  收紧后还拦得住？ False   调 conn.truncate("orders") 把订单表清掉
危险  收紧后还拦得住？ False   session.truncate(Orders) 清空测试数据
危险  收紧后还拦得住？ True    TRUNCATE TABLE orders
```

拿两个真·清表换一个良性函数名，方向正好反了。ORM 里清表本来就是函数调用，
「带括号 = 安全」这个前提在这个域里不成立。

所以**不收紧**，把反例写进 `guard.py` 的注释和两个测试里
（`test_orm_style_table_truncation_is_still_caught` 钉危险的那两句，
`test_a_benign_function_named_truncate_is_a_known_false_positive` 记下现状
而不是掩盖它）。误报的代价是人看一眼 —— `[guard-ops]` 拦下的都落
`needs-human`，本来就要人看。

要不要收紧看数据不看直觉：`factory queue --history` 里的 `[guard-ops]` 命中数
配上 `gate_overruled`（人放回 inbox 的次数）才是判据。而这个判据只在
`[declared-ops]` 不再被 guard 命中数污染之后才成立 —— 这也是为什么归属这个
修必须先落地。

## P1 落地：spec §5 的 `commit` 字段不再永远是 None（2026-08-08）

这是 P0 就写在 spec §5 里、但一直空着的一个字段。`record_result` 的调用点
从第一天起就是 `commit=None`（见上文 dispatcher 代码块），于是 spec 写明的
**漏报回查路径**没有任何数据可走：

> `linked_defects` 捕获假阴性——四道监工都放过、事后才炸的问题。发现 bug 时靠
> git blame → commit → task_id 反查当时裁决，才知道本该哪个监工拦住。

`diff_hash` 顶不上它。diff_hash 是内容指纹：给你一行出问题的代码，你没有
任何办法从它反推到一个 sha256。git blame 给的是 commit sha，所以链条上缺的
就是这一环。

还有一个更直接的症状：判绿的产出一直是 worktree 里**一堆未提交的改动**。
`git worktree remove` 会把它们连带扔掉，而 CLI 打出来的正是
「看完后手动 `git worktree remove <path>`」。

### 三条边界

1. **只在 linked worktree 里提交，主工作树一律拒绝。** 判据是
   `--git-dir != --git-common-dir`（比「.git 是文件还是目录」稳，submodule
   的 .git 也是文件）。这条是防线不是优化：`factory run` 不加 `--worktree`
   时 workspace 就是人的仓库本身，所以这条路径**默认会走到**。人的检出目录
   里冒出一个没人要求过的 commit，是这一层能造成的最坏后果。

   顺带一个不显然的细节：拒绝那条路径必须在 `git add` **之前**返回。先 add
   再检查会把人工作目录的 index 弄脏 —— 他下一次 `git commit` 会连带提交
   agent 的改动，而他以为自己只提交了手写的那部分。

2. **只提交合并的那一轮。** 硬理由不是「打回的产出没人查」，而是
   `capture_diff` 用的是 `git diff HEAD`：中途提交会让下一轮的 diff 变成
   「相对上一轮的增量」而不是「这个任务改了什么」—— 审计里 `diff_hash` 的
   含义会在多轮任务上悄悄换掉，而这个变化在测试里看不出来。

3. **落地失败不改判决。** 退化后果只是 `commit` 仍为 None，也就是这个功能
   存在之前的状态。让一个已经全绿的任务因为 `user.email` 没配变成
   escalated，是拿真问题换假问题。

不碰 git config，不加 `--no-verify`：仓库的 pre-commit hook 该跑就跑，挂了
就是提交失败（第 3 条）。身份缺失时只用 `-c` 临时注入，不写进任何配置文件 ——
落 config 的后果是无人循环悄悄改了人的仓库设置，而 `.git/config` 不被版本
控制，这个改动在 diff 里看不见。

### 回查那一跳：`factory defect --commit`

`factory defect` 原来只收 `attempt_id`。但回查路径的起点是 git blame，那里
**只有 sha** —— 逼人先 `factory show` 一遍才能拿到 attempt_id，等于给漏报
统计加一道摩擦，而需要额外动作的度量等于没有度量。

```
factory defect --commit 3f9a2b1 BUG-42
→ commit 3f9a2b1 → task_id=T-add-titlecase-1 attempt #1 (id=1)
  attempt 1 defects=['BUG-42']
    当时 [regression] 判了 pass
    当时 [scope] 判了 pass
    当时 [risk] 判了 pass
```

最后那几行是刻意加的：漏报的意义在于「本该哪个监工拦住」，不打出来人还得
再敲一次 `show`。

短 sha 接受前缀匹配。**前缀撞车时返回 None 而不是随便挑一条**：挂错 attempt
的 defect 会把漏报记到无关的监工头上，比查不到更糟 —— 查不到人会再试，
挂错了没人知道，而 spec §5.1 的漏报数正是裁剪监工的判据。

### 一个测试抓出的真 bug

`_identity_args` 原来写的是 `[f"-c=user.name={...}", ...]`。git 报
「未知选项：-c=user.name=factory」—— `-c` 必须是两个 argv。

这条路径**只在身份完全没配时才走到**，而所有测试 fixture 都配了
`user.email`，所以它一次都没被执行过。抓到它的测试是
`test_a_repo_with_no_identity_anywhere_still_lands`，关键在于它把
`GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` 都指到空文件 —— 不隔掉全局配置，
本机跑起来永远走不到兜底分支，而 CI 容器里会。

### 变异测试

两条承重路径各故意破坏一次：

| 破坏 | 该挂的测试 |
|---|---|
| `if not is_linked_worktree(root)` → `if False` | `test_a_commit_is_never_made_in_the_main_worktree`、`test_a_failed_landing_does_not_change_the_verdict` |
| 把 `land()` 挪到每一轮都跑 | `test_a_merged_attempt_records_the_commit_sha`、`test_reworked_rounds_are_not_committed` |

第二条第一次跑的时候**没挂**。原因是 `test_reworked_rounds_are_not_committed`
写的是 `rows[1].commit == report.commit` —— 两边都是 None 时这条也成立。而
「每轮都提交」这个缺陷的症状恰恰是：第一轮把改动吃掉，合并那轮无改动可提交，
两边一起变 None。加上 `assert report.commit is not None` 之后才真的挂。

一个在缺陷存在时仍然通过的断言，比没有断言更糟。

### 真跑抓到的：`add -A` 让 commit 和 diff_hash 描述不同的东西

第一次真跑就出问题。任务是给 `src/text.py` 加一个 `titlecase`，merged、
$0.4874、三监工全 pass、sha 落库了。但 `git show --stat` 是这样的：

```
 src/__pycache__/__init__.cpython-312.pyc | Bin 0 -> 171 bytes
 src/__pycache__/text.cpython-312.pyc     | Bin 0 -> 510 bytes
 src/text.py                              |   4 ++++
```

那两个 `.pyc` 是**我们自己的 check 命令**留下的 —— 生成的 check 是
`python3 -c "from src.text import titlecase; ..."`，一跑就写 `__pycache__`。

问题不在「多了两个垃圾文件」。`capture_diff` 算 diff_hash 时看到的
`changed_paths` 只有 `src/text.py`，而 commit 里多了两个文件。也就是说
**审计的两个承重字段开始描述不同的内容**：监工审的是 diff_hash 覆盖的那一份，
出货的是 commit 那一份，中间那段差额没有任何人看过。

实测确认了这个错配：把 commit 里 `src/text.py` 的 diff 单独 sha256，

```
text.py only sha256: d123efc6dba85f922d34d98ffd0a71ab4867626b52ab67aaedee02a445196f9e
库里记的 diff_hash  : d123efc6dba85f922d34d98ffd0a71ab4867626b52ab67aaedee02a445196f9e
```

一模一样 —— 两者相差的正好就是那两个 `.pyc`。

改法是把 `changed_paths` 传给 `land()`，`git add -- <paths>` 而不是
`add -A`：**提交的必须正好是监工审过的那一组。** `.pyc` 留在工作区里没被删 ——
删它不是这一层的事。

`paths` 为空时退化成 `add -A`，给不报 changed_paths 的 harness（shell adapter
那类）留路。这是刻意的退化：那条路径上「提交多了」好过「什么都没提交」，
因为 diff_hash 在那里本来也是全量算的。

**550 个测试全绿，没有一个能抓到它。** 抓到它的是一次真跑 —— 单元测试里
没有任何东西会去跑 `python3 -c`，所以 `__pycache__` 永远不会出现。这一条
补了三个回归测试（一个在 `land()` 层，一个在 dispatcher 接线层，一个钉
「删除的文件也要能落地」），并各自变异验证过。

补一条那三个回归测试都没覆盖的时序：它们全从**干净 index** 出发，而真管线
里 `land()` 拿到的 index 是脏的 —— `capture_diff` 为了让 diff 包含未跟踪文件，
先跑过一次 `git add -A -N`，把所有未跟踪文件（含 `.pyc`）登记成 intent-to-add。
从空 index 出发的测试哪怕退回 `add -A` 也照样绿。

用 /tmp 探针先确认了 git 的语义：intent-to-add 的行**不会**被后续
`git commit` 带进去，所以修法在真时序下依然成立。

```
--- simulate capture_diff ---
 A src/__pycache__/text.pyc
 A src/text.py
=== committed files ===
src/text.py
```

`test_an_index_already_polluted_by_capture_diff_still_commits_only_paths` 把这个
时序钉住了，并且和 `test_only_the_reviewed_paths_are_committed` 一起变异验证：
把 `add -- <paths>` 改回 `add -A`，两条同时挂。

### 判据自己漏了一项（第二次）

上面那条「§5 字段全部有值」的清单里补了 `commit` 之后，才发现核对表还缺
**这个字段的内容对不对**。有值不等于对：`add -A` 那个缺陷下 `commit` 是有值的、
40 位的、能反查的 —— 只是它描述的内容比监工审过的多两个文件。所以核对表又加了
两条，都只有真跑能查：

- 提交的文件集正好等于 `diff_hash` 的来源，且主工作树未被写入
- 短 sha 能反查回 attempt

同一张核对表在同一个字段上漏了两次（先漏字段，再漏字段的正确性）。这比实现
漏项难发现得多，因为核对表读起来一直是绿的。

### 端到端判据一直没覆盖 commit —— 因为它没开 worktree

`test_class_a_task_end_to_end` 原来不加 `--worktree`，于是 workspace 就是
临时仓库本身，`land()` 会（正确地）拒绝在主工作树提交 —— `commit` 永远是
None。P0 判据当年漏掉 commit，和这个是同一个原因：**唯一能查它的那条测试
恰好走在拒绝分支上。**

改成 `--worktree --worktree-root`，并把三条断言加进去：产出在 worktree 里、
`HEAD == row.commit`、`git show --name-only` 正好是 `greet.py`、主工作树
`git status` 干净、短 sha 能反查回 attempt。

`.pyc` 那个错配的判据放在这条真跑里而不是单元测试里，是因为它只在真的执行
生成的 check 命令时才出现。

### 整个测试套件收集失败，和这次的改动无关

跑全量时两个模块直接 ImportError：

```
tests/test_dispatcher_four.py: No module named 'tests.test_dispatcher'
tests/test_scope.py:           No module named 'tests.test_dispatcher'
```

这两个模块 `from tests.test_dispatcher import ...` 复用夹具。查下去发现
`tests` 解析到了别的地方：

```
tests -> /opt/miniconda3/lib/python3.12/site-packages/tests/__init__.py
```

site-packages 里有个第三方库（conda / ultralytics / google-search-results
的 RECORD 里都有 `tests/`）装了一个**顶层 `tests` 包**。我们的 `tests/` 没有
`__init__.py`，只是命名空间包 —— 而命名空间包只在整条 sys.path 扫完都没找到
真包时才生效，所以那个带 `__init__.py` 的赢了。

加一个 `tests/__init__.py` 钉死。不是风格问题：**装了哪些第三方库不该决定
这个仓的测试能不能收集。** 之前能跑纯属那个包还没装上。

556 个测试全绿（`-m "not smoke"`）。

改完的真跑（375s，一次通过）：

```
task     : T-smoke-1
outcome  : merged
rounds   : 1
class    : A  (no rule matched -> default A)
commit   : 6a2323ca9af6217b957c0e167b227600695980b3  (分支上已提交，主分支未动)
```

五条新断言全部成立：40 位 sha、`HEAD == row.commit`、`git show --name-only`
正好 `greet.py`（`.pyc` 没漏进去）、主工作树 `git status` 干净、短 sha
`attempt_by_commit` 反查回同一个 attempt。至此 spec §5 的字段清单**没有一个
是恒为 None 的**，漏报回查路径端到端有数据可走。

## P1 定时启动：「无人」原来只做到「起跑后无人」（2026-08-08）

复核时发现的边界问题：`loop` 常驻也好、drain 也好，**都得人敲一次**。仓库里
没有任何调度产物（`launchd` / `crontab` / plist 一个都没有），所以「无人」的
实际含义一直是「起跑之后无人」，而不是「不需要人起跑」。

补这一格的路上撞到一个真故障，而它比调度本身重要。

### launchd 的 PATH 里没有 claude

```
$ command -v claude
/Users/auntlee/.nvm/versions/node/v24.16.0/bin/claude

$ env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin sh -c 'command -v claude'
NOT FOUND (launchd 默认 PATH 下)
```

`claude` 装在 nvm 管的目录下，而 launchd 给的默认 PATH 是那四个系统目录。
照着「怎么用」那节抄一个 plist 出来，夜跑会**每一次派发都失败**。

难查的地方不在 PATH 本身，在于这个故障没有早期信号：

- `build_adapter` 只校验 harness 的**名字**（`claude_code` / `shell`），
  从不校验 binary 能不能跑；
- `ClaudeCodeAdapter.version()` 吞掉 `OSError` 回 `"unknown"` —— 那是对的，
  探针失败不该杀掉一次运行，但也意味着这里不会报警；
- 于是队列被逐个刷成 `error`，第二天早上看到的是「昨晚 12 个任务全异常」，
  指向哪儿都可能。

这正是 `_cmd_loop` 自己的 docstring 写着要防的那类失败（「循环一旦跑起来就
没人看着了，第 40 分钟才因为一个拼错的路径退出是最贵的失败方式」）——
只是当时把 binary 这一格漏了。

### `factory/harness/preflight.py` 的四个决定

1. **只在 `loop` 的入口查，不在 `run` 里查。** `run` 是人敲的，敲错了当场就
   看见报错；`loop` 没人看着。给 `run` 也加等于给最常用的路径加一次无谓的
   `which`，而它换来的信息人已经有了。

2. **查在 `Backlog.ensure()` 之前。** 不只是洁癖：`queue` 子命令看到目录存在
   就认为队列已初始化，人会以为「队列是好的，任务没进来」，而真相是 loop
   一次都没跑起来。一次配置错误不该留下半初始化的目录。

3. **`--harness shell` 时查 `shell_argv[0]`，不查 `ns.binary`。** 查 binary
   会让 shell harness 在没装 claude 的机器上过不了预检（它根本不需要
   claude）；反过来，脚本不存在却因为 claude 在就放行 —— 预检对这条路径
   完全失效。这一条被变异测试验过：改成永远查 `ns.binary`，
   `test_loop_preflight_checks_shell_argv_not_the_claude_binary` 挂。

4. **含斜杠时按路径处理，不查 PATH**，和 execvp 语义一致。否则
   `--binary ./wrapper.sh` 会被拿去 PATH 里搜一遍，然后报一个误导人的
   「不在 PATH 里」—— 而真正的问题是相对路径相对错了目录。

报错必须带上**当时的 PATH**：这个故障的全部难点就是「PATH 和我以为的不一样」，
只说「找不到 claude」会让人去查安装，而装是装好的。`test_..._names_the_path_
it_searched` 钉住了这一点，连「得点出定时任务 PATH 更短」这句提示都在断言里。

还钉了一条会过期的测试：`test_the_real_launchd_path_does_not_find_claude`
断言在那四个系统目录下必须抛。哪天 claude 真装进 `/usr/bin`，这条会挂 ——
那正是该重读这个模块还有没有必要的时候。

### `examples/launchd/com.factory.loop.plist`

给出来的是**样例不是即用件**，所有需要按本机改的行都标了「←」。三个会让夜跑
静默失效的设置：

| 设置 | 为什么必须这样 |
|---|---|
| `EnvironmentVariables.PATH` 含 nvm 的 bin | 上面那个故障 |
| `StandardOutPath` / `StandardErrorPath` 分开两个文件 | 不设的话 stdout 进 /dev/null，「昨晚跑得怎么样」只能靠 audit.db 反推，而**队列侧的时间不在库里**；合在一个文件里则正常日报和异常栈没法分开 grep |
| `RunAtLoad` = `false`，且**不设** `KeepAlive` | RunAtLoad 会让每次登录都多跑一轮 —— 按天算的预算被登录次数触发，`--budget-usd` 就失去意义。KeepAlive 会把 drain 模式的正常退出当崩溃再拉起来，变成无限循环 |

`--idle drain` 而不是 `watch`：定时任务该跑完就退，常驻才用 watch。

写这个文件时自己踩了一次 XML 的坑：注释里嵌 `<!-- ← -->` 让整个 plist 解析
失败（XML 注释不能嵌套）。`plutil -lint` 一句话就指出了行号，而 launchctl
只会说加载失败、不说哪一行 —— 所以文件头上写了「改完先跑 plutil -lint」。

### 用 plist 的真实 argv 验过两遍

不是读一遍就算数。把 plist 里的 argv 和 env 原样跑出来：

```
# launchd 的默认 PATH → 预检拦住，队列目录一个都没建
PATH 里找不到 worker 可执行体 `claude`。
  当前 PATH: /usr/bin:/bin:/usr/sbin:/sbin
--- queue created? ---
nothing created

# plist 里那条 PATH → 起得来
worker   : /Users/auntlee/.nvm/.../@anthropic-ai/claude-code/bin/claude.exe
queue=/private/tmp/plisttest/q  workspace=/tmp/plisttest/ws  idle=drain
== 停机：队列已抽干
派发 0：合并 0 / 升级 0 / 硬闸门 0 / 异常 0，花费 $0.0000
```

顺带确认了 `.venv/bin/python -m factory.cli` 这条入口本身可用（没有
`__main__.py`，靠 `cli.py` 末尾的 `if __name__` 生效），以及 `worker` 那行
打出的是符号链接背后的真实路径。

`_cmd_loop` 之前**一个 CLI 层测试都没有** —— 它 docstring 里写明存在理由的
那段前置校验，此前没有任何东西验证。现在有三条，且两次变异（改成永远查
`ns.binary`、把预检挪到 `ensure()` 之后）分别挂 1 条和 3 条。

569 个测试全绿。

### 写 README 时发现：`factory` 这个命令一直不存在

设计文档从第一天就写着 `factory run ...` / `factory loop ...`，但：

```
$ uv run factory --help
error: Failed to spawn: `factory`
  Caused by: No such file or directory (os error 2)
```

`pyproject.toml` 里没有 `[project.scripts]`。也就是说本文档、plist、以及所有
「怎么用」示例里的每一条命令都跑不起来 —— 而**没有任何测试会发现**，因为
测试全部直接 `from factory.cli import main`，绕过了入口点。

补入口点而不是改文档：文档描述的是意图中的接口，而 `python -m factory.cli`
这种写法一旦进了文档就会渗到 plist、cron、README 的每一行去。加了
`[project.scripts]` 和 hatchling 的 build-system（之前连 build-system 都没有，
所以这个包从来没被真正安装过，靠 `pythonpath = ["."]` 撑着）。

两条测试钉住它：一条断言 `[project.scripts]` 的声明，一条断言 `main()` 返回
**int** —— 返回 None 会被 `sys.exit(None)` 变成退出码 0，于是 cron 认为
「失败的那次也成功了」。

plist 随之改成直接用 `.venv/bin/factory`：绝对路径的 console script 自带正确
的 sys.path，不依赖 `WorkingDirectory`。改完用它的真实 argv 又验了一遍
（`worker` 行正常、队列抽干、退出 0），launchd 默认 PATH 下仍然退 2 且不建目录。

### `README.md`

之前没有 README，唯一的入口是这份 4700 行的建造日志 —— 它按时间顺序记决定和
理由（包括后来被证伪的判断），当手册用是错的。README 只回答「怎么跑」，
并把「为什么是这样」指回这里。

里面刻意重复了三条边界（只在 linked worktree 提交 / 只提交审过的那一组 /
落地失败不改判决）。文档去重在这三条上是错的优化：它们是改代码时最容易
"顺手放宽"的地方，而放宽的后果分别是往人的仓库里写 commit、审计两个字段
描述不同内容、以及把全绿的任务判成升级。

### 第二次真跑抓到：超时的 attempt 记 $0，而它真的花了钱

跑 README 的 quickstart 验证命令能不能用（`examples/greet_task.yaml` →
一个 scratch 仓库），顺带撞到一个预算漏账。审计库里是这样的：

```
-- attempt #1  (id=1)   model=haiku
   commit     : -
   tokens     : in=0 out=0  cost=$0.0000  900058ms
   resolution : reworked
   [regression] fail  - harness: 期望 'exit_status ok' / 实得 'timeout: timeout after 900s'

-- attempt #2  (id=2)   model=sonnet
   commit     : e5bdc932f3ec...
   tokens     : in=595112 out=44838  cost=$2.4579  60745ms
   resolution : merged
```

第一轮 900 秒超时、记 **$0.0000**。但它的 transcript 还在磁盘上，数一遍：

```
usage 行数=8  in≈232616  out≈802
带 cost 的字段: (无)
```

也就是说它烧了约 23 万 input tokens，而预算闸门看到的是 0。原因很直接：花费
只来自 claude CLI 的 JSON payload（`total_cost_usd`），而**被我们 kill 掉的
进程永远不会打出那个 payload**，超时分支走的是 `_result(...)` 的默认
`cost_usd=0.0`。

这正是本文档上面已经写过的那条原则又踩了一次：「低估的预算闸门等于没有闸门」。
当时说的是监工花费记在 verdict 行上要一起加，没想到超时是同一类漏账 ——
而它更隐蔽，因为 `--budget-usd` 是无人循环**唯一的默认上限**
（`--max-tasks` 和 `--max-runtime` 默认都是 0=无限）。一个反复超时的任务
在预算眼里是免费的。

**没有去估算价格。** transcript 里没有 cost 字段，按 token 估需要一张价目表，
而价目表会过期 —— 估错的账单比没有账单更难查（人会拿它去对月账）。改成在
`_attempts_cost()` 里对「$0 且 claim 里有 timeout」的 attempt 打一行警告，
带上 transcript 路径，让人能自己核。警告不参与任何判决。

判据用 claim 文本里的 `timeout` 而不是加一列状态：超时在审计库里本来就只以
这个形式存在（`实得 'timeout: timeout after 900s'`）。文本判据不好看，但比
在审计模型上加一列小得多，而这一行只是警告。

四条测试，两次变异各挂一条：去掉 `if row.cost_usd: return`（付了钱的也报警）
挂 `test_an_attempt_that_actually_cost_money_...`；把 timeout 匹配改成 `if True`
（确定性监工的 $0 attempt 也报警，噪音等于没有警告）挂
`test_a_zero_cost_attempt_that_did_not_time_out_...`。

补一道墙钟闸。既然超时能让预算读成 $0，那预算就不是充分的上限，而单个任务
最坏是 `max_rounds x --timeout` = 3 x 900s = 45 分钟，之后循环继续认领下一个：

- plist 里加 `--max-runtime 14400`（4 小时）并注明理由 —— 墙钟是超时唯一
  躲不过的那道闸；
- `loop` 在 `--max-runtime 0`（默认值）时打警告，和 `--budget-usd 0` 的警告
  并列。两条都只是警告不是拒绝：夜跑该拦，前台调试不该被拦。

**没有**给单个 attempt 加墙钟停机（超时本身触发 loop 停机）。那是更彻底的修法，
但它会把「一个任务超时」升级成「整个夜跑停摆」—— 在漏账和停摆之间，先选能看见
的漏账。这条留在这里，是因为将来若真要堵死这个洞，方向是那个而不是按 token
估价（价目表会过期，估错的账单比没有账单更难查）。
