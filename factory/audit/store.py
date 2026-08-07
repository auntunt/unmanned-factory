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
    Base,
    OracleClass,
    Resolution,
    SupervisorRole,
    SupervisorVerdict,
    TaskAttempt,
    Verdict,
)
from factory.redact import redact


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
        # in-memory 时同一个 engine 内的连接池会复用同一条连接，
        # 所以同一 AuditStore 实例里的多次 Session 看到的是同一个库。
        self._engine = create_engine(url)
        Base.metadata.create_all(self._engine)

    def _session(self) -> Session:
        return Session(self._engine)

    def _max_attempt_no(self, s: Session, task_id: str) -> int:
        return s.scalar(
            select(func.max(TaskAttempt.attempt_no)).where(
                TaskAttempt.task_id == task_id
            )
        ) or 0

    def next_attempt_no(self, task_id: str) -> int:
        with self._session() as s:
            return self._max_attempt_no(s, task_id) + 1

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
            row = TaskAttempt(
                task_id=task_id,
                attempt_no=self._max_attempt_no(s, task_id) + 1,
                spec_ref=spec_ref,
                oracle_class=oracle_class,
                class_reason=redact(class_reason),
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
        harness_version: str | None = None,
    ) -> None:
        """落一次 attempt 的执行结果。

        harness_version 在 open_attempt 时就写了一次，但真实版本号往往要等
        adapter 跑完才知道（比如从 CLI 输出里读到）。所以这里允许非 None 覆盖写。
        """
        with self._session() as s:
            row = s.get(TaskAttempt, attempt_id)
            row.diff_hash = diff_hash
            row.commit = commit
            row.transcript_path = transcript_path
            row.tokens_in = tokens_in
            row.tokens_out = tokens_out
            row.cost_usd = cost_usd
            row.wall_clock_ms = wall_clock_ms
            if harness_version is not None:
                row.harness_version = harness_version
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
            s.add(
                SupervisorVerdict(
                    attempt_id=attempt_id,
                    role=role,
                    verdict=verdict,
                    # claims 直接来自 check 命令的 stdout/stderr —— 唯一可能
                    # 携带明文密钥的入口。脱敏放在这里而不是各 supervisor 里，
                    # 和 D 类硬闸门同理：不留旁路。
                    claims=redact(claims),
                    tokens=tokens,
                    cost_usd=cost_usd,
                )
            )
            s.commit()

    def escalate_class(
        self, attempt_id: int, *, oracle_class: OracleClass, class_reason: str
    ) -> None:
        """后分级比预分级更严时改写。class_reason 覆盖写，保留判定依据可回溯。"""
        with self._session() as s:
            row = s.get(TaskAttempt, attempt_id)
            row.oracle_class = oracle_class
            row.class_reason = redact(class_reason)
            s.commit()

    def finalize(self, attempt_id: int, resolution: Resolution) -> None:
        with self._session() as s:
            s.get(TaskAttempt, attempt_id).resolution = resolution
            s.commit()

    def link_defect(self, attempt_id: int, defect_id: str) -> None:
        """幂等：同一个 defect 重复挂只留一条。

        漏报数直接来自 len(linked_defects)，重复计数会让 spec §5.1 的
        「漏报数」虚高，进而把一个其实在干活的监工判成没干活。
        """
        with self._session() as s:
            row = s.get(TaskAttempt, attempt_id)
            if defect_id in row.linked_defects:
                return
            # JSON 列必须整体重新赋值，原地 append 不会被 ORM 检测为脏
            row.linked_defects = [*row.linked_defects, defect_id]
            s.commit()

    def exists(self, attempt_id: int) -> bool:
        with self._session() as s:
            return s.get(TaskAttempt, attempt_id) is not None

    def get(self, attempt_id: int) -> TaskAttempt:
        with self._session() as s:
            row = s.scalar(
                select(TaskAttempt)
                .options(selectinload(TaskAttempt.supervisors))
                .where(TaskAttempt.id == attempt_id)
            )
            s.expunge_all()
            return row

    def all_attempts(self, *, task_id: str | None = None) -> tuple[TaskAttempt, ...]:
        """全库（或单任务）的 attempt，含 supervisors。给 spec §5.1 报表用。"""
        with self._session() as s:
            stmt = (
                select(TaskAttempt)
                .options(selectinload(TaskAttempt.supervisors))
                .order_by(TaskAttempt.id)
            )
            if task_id is not None:
                stmt = stmt.where(TaskAttempt.task_id == task_id)
            rows = tuple(s.scalars(stmt))
            s.expunge_all()
            return rows

    def attempts_for(self, task_id: str) -> tuple[TaskAttempt, ...]:
        """按 attempt_no 升序返回某个任务的全部 attempt，供 CLI show 用。"""
        with self._session() as s:
            rows = tuple(
                s.scalars(
                    select(TaskAttempt)
                    .options(selectinload(TaskAttempt.supervisors))
                    .where(TaskAttempt.task_id == task_id)
                    .order_by(TaskAttempt.attempt_no)
                )
            )
            s.expunge_all()
            return rows
