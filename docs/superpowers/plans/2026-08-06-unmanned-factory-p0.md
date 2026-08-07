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
      `transcript_path` / `tokens` / `cost` / `resolution`
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
  `sandbox=False` 参数）。
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
   沙箱更危险。

审计留痕走 `harness_version` 后缀（`2.1.224 (Claude Code)+sandbox`），不加列：加列要改
schema，而改 schema 本身是 D 类不可逆操作 —— 为了记一个布尔值去动硬闸门管辖的东西不值得。
没有这个后缀，两次 `merged` 长得一模一样，事后分不清哪个产出是在隔离下拿到的。
