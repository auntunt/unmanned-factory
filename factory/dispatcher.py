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
from factory.harness.workspace import neighbour_context
from factory.runbook import RunbookLibrary
from factory.supervisors.architecture import ArchitectureSupervisor
from factory.supervisors.base import SupervisorReport
from factory.supervisors.model_base import SUPERVISOR_ERROR_PREFIX
from factory.supervisors.regression import RegressionSupervisor
from factory.supervisors.scope import ScopeSupervisor
from factory.supervisors.spec_review import SpecSupervisor
from factory.task import Task


class Outcome(StrEnum):
    MERGED = "merged"
    ESCALATED = "escalated"
    BLOCKED_HARD_GATE = "blocked_hard_gate"


@dataclass(frozen=True)
class Merged:
    """一轮里所有监工裁决的合并结果。

    feedback 和 faults 分开，因为去处不同：feedback 打回 worker，
    faults 直接升级给人（worker 修不了监工的故障）。
    """

    blocking: bool
    feedback: tuple[dict, ...] = ()
    faults: tuple[dict, ...] = ()


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
        spec_supervisor: SpecSupervisor | None = None,
        architecture_supervisor: ArchitectureSupervisor | None = None,
        runbook: RunbookLibrary | None = None,
        limits: Limits | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._engine = engine or GradingEngine.default()
        self._router = router or Router.default()
        self._supervisor = supervisor or RegressionSupervisor()
        # 范围监工默认开启，和两个模型监工相反 —— 它零成本、确定性，而且
        # declared_paths 为空时一律 PASS，所以「默认开」不会给既有任务加新的红。
        self._scope = ScopeSupervisor()
        # 两个调模型的监工默认关闭（None）。它们每轮都要花钱，而 P0 已证明
        # 确定性监工零成本就能跑通 A 类任务 —— 默认开启会让最便宜的路径变贵。
        self._spec = spec_supervisor
        self._architecture = architecture_supervisor
        # runbook 规则库（spec §8）。默认 None = 只跑任务自带的 check。
        # 不默认加载全局规则：那会让每个既有任务的检查集在升级工厂后悄悄变大，
        # 突然多出来的打回没人能对上原因。要继承得显式说。
        self._runbook = runbook
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

            reports = self._review(task, workspace, result)
            for report in reports:
                self._store.record_verdict(
                    aid,
                    role=report.role,
                    verdict=report.verdict,
                    claims=list(report.claims),
                    tokens=report.tokens,
                    cost_usd=report.cost_usd,
                )

            is_last = round_no == task.max_rounds
            merged = self._merge_reports(reports, is_last=is_last)

            if not merged.blocking:
                self._store.finalize(aid, Resolution.MERGED)
                return DispatchReport(
                    Outcome.MERGED, tuple(attempt_ids), round_no, grade
                )

            if merged.faults:
                # 监工自己坏了，重试也是坏的。当场上人，不浪费剩余轮次。
                self._store.finalize(aid, Resolution.ESCALATED)
                return DispatchReport(
                    Outcome.ESCALATED,
                    tuple(attempt_ids),
                    round_no,
                    grade,
                    "监工不可用，未完成审查（不打回 worker）：\n"
                    + self._render(merged.faults),
                )

            feedback = merged.feedback
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

    @staticmethod
    def _merge_reports(
        reports: tuple[SupervisorReport, ...], *, is_last: bool
    ) -> Merged:
        """合裁决。回归/规格是硬的，架构是软的，监工自身故障是第三类。

        架构监工出的是意见不是证据（spec §4.1），没有客观裁判能证明它对。
        让意见能否决合并，等于给一个爱挑刺的监工一票否决权，每个任务都能被
        拖到三轮上人 —— P1 判据「上人平均打回次数 ≤ 1」当场就废了。
        所以：它的意见前两轮当返工建议带回去，最后一轮不再拦，照样入库留证。

        监工故障（超时、拿不到裁决）要拦住合并 —— 没审过不等于没问题 ——
        但**不能打回 worker**：worker 修不了监工的故障，那会白烧三轮。
        所以故障单独拎出来，直接升级给人。
        """
        faults: list[dict] = []
        hard: list[dict] = []
        soft: list[dict] = []

        for r in reports:
            for c in r.claims:
                if str(c.get("check", "")).startswith(SUPERVISOR_ERROR_PREFIX):
                    faults.append(c)
                elif r.role == SupervisorRole.ARCHITECTURE:
                    soft.append(c)
                else:
                    hard.append(c)

        feedback = list(hard)
        if soft and not is_last:
            feedback.extend(soft)
        return Merged(
            blocking=bool(feedback) or bool(faults),
            feedback=tuple(feedback),
            faults=tuple(faults),
        )

    def _review(self, task, workspace, result) -> tuple[SupervisorReport, ...]:
        """harness 报错 / 空 diff 都算这一轮红，且不跑 check、不调模型监工。

        原因：check 全绿但 agent 什么都没改，说明 check 太弱或任务已完成，
        两种都需要人看一眼，不能静默 merge。没有 diff 时调模型监工纯烧钱。
        """
        if not result.ok:
            return (
                self._blocked(
                    "harness",
                    self._adapter.name,
                    "exit_status ok",
                    f"{result.exit_status.value}: {result.error_text}",
                ),
            )
        if not result.changed_paths:
            return (
                self._blocked(
                    "diff",
                    "git diff HEAD",
                    "至少一个文件改动",
                    "no changes produced",
                ),
            )

        checks = task.checks + self._runbook_checks(workspace, result)
        reports = [
            self._supervisor.review(workspace, checks),
            # 范围监工放在回归监工旁边而不是后分级旁边：后分级问「危不危险」，
            # 越界问「是不是离题」。越界是 worker 自己能修的（把无关文件改回去），
            # 所以它必须走打回路径，而后分级的升级路径是不打回的。
            self._scope.review(
                changed_paths=result.changed_paths,
                declared_paths=task.declared_paths,
            ),
        ]
        # 两个监工都要看周边既有代码，算一次共用：规格监工用它查 diff 引用到的
        # 实现，架构监工用它判约定和重复实现。
        context = neighbour_context(Path(workspace), result.changed_paths)

        if self._spec is not None:
            # 扣掉的输入显式传进去校验：build log 和上一轮监工结论都不许进 prompt。
            # spec §4.1「不给 build log」在这里是可执行约束，不是注释。
            reports.append(
                self._spec.review(
                    diff=result.diff,
                    # task.criteria = spec_ref + acceptance。口述来源的任务
                    # 没有外部文档可引，验收标准写在 acceptance 里。
                    criteria=task.criteria,
                    context=context,
                    withheld=(result.error_text, self._render(reports[0].claims)),
                )
            )
        if self._architecture is not None:
            reports.append(
                self._architecture.review(
                    diff=result.diff,
                    context=context,
                    withheld=(result.error_text, self._render(reports[0].claims)),
                )
            )
        return tuple(reports)

    def _runbook_checks(self, workspace: Path, result) -> tuple:
        """runbook 规则库选出的检查，接在任务自带 check 后面。

        走回归监工而不是新开一个监工角色：这些规则是**确定性命令**，
        和回归监工的裁决方式完全一样（跑命令、比对输出、零模型调用）。
        另立一个角色只会让 §5.1 的命中率报表多一个分母，而这两类检查
        的失败对 worker 来说是同一件事 —— 都是"具体哪条命令没过"。

        规则库自身出问题（YAML 坏了、路径不存在）**不吞**：那是配置错误，
        静默跳过等于人以为规则生效了而实际没有。让它往上抛，当场失败。
        """
        if self._runbook is None:
            return ()
        selection = self._runbook.select(tuple(result.changed_paths), workspace)
        return selection.checks

    def _blocked(
        self, check: str, command: str, expected: str, got: str
    ) -> SupervisorReport:
        return SupervisorReport(
            role=SupervisorRole.REGRESSION,
            verdict=Verdict.FAIL,
            claims=({"check": check, "command": command,
                     "expected": expected, "got": got},),
        )

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
            "上一轮被监工打回。以下是具体失败项，逐条修掉，不要改动无关文件：\n"
            f"{self._render(feedback)}\n"
        )
