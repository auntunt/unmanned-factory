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
    NOT_DISPATCHED,
    Resolution,
    SupervisorRole,
    Verdict,
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
            harness_version=NOT_DISPATCHED,
            model=model,
        )
        self._store.record_verdict(
            aid,
            role=SupervisorRole.RISK,
            verdict=Verdict.FAIL,
            claims=[
                {
                    "check": "pre-dispatch-grading",
                    "command": "",
                    "expected": "class A/B (unmanned allowed)",
                    "got": grade.reason,
                }
            ],
        )
        self._store.finalize(aid, Resolution.ESCALATED)
        return aid

    def run(self, task: Task, workspace: Path) -> DispatchReport:
        pre = self._engine.grade(task.declared_paths, task.declared_ops)

        if not pre.unmanned_allowed:
            model = self._router.model_for(pre.oracle_class, 1)
            aid = self._record_blocked(task, pre, model)
            outcome = (
                Outcome.BLOCKED_HARD_GATE if pre.hard_gate else Outcome.ESCALATED
            )
            reason = f"pre-dispatch {pre.oracle_class.value}: {pre.reason}" + (
                " —— 硬闸门，agent 只能生成待执行脚本" if pre.hard_gate else ""
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

            # 后分级（= 风险监工）：用真实改动的文件再判一次
            post = self._engine.grade(result.changed_paths, task.declared_ops)
            if post.more_severe_than(grade):
                grade = post
                escalated_reason = f"post-diff escalation: {post.reason}"
                self._store.escalate_class(
                    aid,
                    oracle_class=post.oracle_class,
                    class_reason=escalated_reason,
                )
                self._store.record_verdict(
                    aid,
                    role=SupervisorRole.RISK,
                    verdict=Verdict.FAIL,
                    claims=[
                        {
                            "check": "post-diff-grading",
                            "command": "",
                            "expected": f"class {pre.oracle_class.value} (declared)",
                            "got": post.reason,
                        }
                    ],
                )
                if not post.unmanned_allowed:
                    self._store.finalize(aid, Resolution.ESCALATED)
                    outcome = (
                        Outcome.BLOCKED_HARD_GATE
                        if post.hard_gate
                        else Outcome.ESCALATED
                    )
                    return DispatchReport(
                        outcome,
                        tuple(attempt_ids),
                        round_no,
                        post,
                        escalated_reason,
                    )
            else:
                self._store.record_verdict(
                    aid, role=SupervisorRole.RISK, verdict=Verdict.PASS, claims=[]
                )

            report = self._review(task, workspace, result)
            self._store.record_verdict(
                aid,
                role=report.role,
                verdict=report.verdict,
                claims=list(report.claims),
                tokens=report.tokens,
                cost_usd=report.cost_usd,
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
            Outcome.ESCALATED,
            tuple(attempt_ids),
            task.max_rounds,
            grade,
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
                role=SupervisorRole.REGRESSION,
                verdict=Verdict.FAIL,
                claims=(
                    {
                        "check": "harness",
                        "command": self._adapter.name,
                        "expected": "exit_status ok",
                        "got": f"{result.exit_status.value}: {result.error_text}",
                    },
                ),
            )
        if not result.changed_paths:
            return SupervisorReport(
                role=SupervisorRole.REGRESSION,
                verdict=Verdict.FAIL,
                claims=(
                    {
                        "check": "diff",
                        "command": "git diff HEAD",
                        "expected": "至少一个文件改动",
                        "got": "no changes produced",
                    },
                ),
            )
        return self._supervisor.review(workspace, task.checks)

    # ---------- 打回时的 prompt ----------

    @staticmethod
    def _render(claims) -> str:
        return "\n".join(
            f"- [{c.get('check')}] 期望 {c.get('expected')!r}，实得 {c.get('got')!r}"
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
