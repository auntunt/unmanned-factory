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
from factory.audit.archive import ArchiveResult, archive_attempt
from factory.audit.store import AuditStore
from factory.checks_contract import (
    contract_text,
    load as load_worker_checks,
    vacuous_checks,
)
from factory.grading.rules import Grade, GradingEngine
from factory.harness.base import AttemptResult, HarnessAdapter, Limits
from factory.harness.landing import land
from factory.routing import Router
from factory.harness.verdict_probe import judge_files_touched, probe
from factory.harness.workspace import neighbour_context
from factory import gates
from factory.intake.guard import harden_ops
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
    # merged 时产出被提交到了哪个 commit。None = 没落地（不在 worktree 里、
    # 没有改动、或者提交失败）。landing_note 说明是哪一种，见 landing.Landing。
    commit: str | None = None
    landing_note: str = ""


_MAX_LISTED_PATHS = 20

#: 连续多少次「worker 起来了但不产出」就判执行环境故障、当场上人。
#:
#: 取 2 而不是 1：单次挂死可能只是中转站抖一下，值得再试一次 —— 实测的
#: 那次派发里，唯一跑通的产出正是重试拿到的。连着两次就不是抖动。
_STALL_STREAK_LIMIT = 2


def _criteria_delta(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[str, ...]:
    """派发时有、现在没了的标准。给 worker 看的是**丢了哪几条**。

    刻意不列「现在多出来的」：改一条正文会同时产生一进一出，两条都摊出来
    worker 看到的是自己写的那句话被引用一遍，容易读成「系统认可了」。
    只说丢了什么，指令才唯一：把它改回去。
    """
    gone = tuple(c for c in before if c not in after)
    return gone or ("（条数没变，正文被改写）",)


def _listed(paths: tuple[str, ...]) -> str:
    """路径列表转成给 worker 看的一行。上限见 supervisors/scope.py 的同名理由：
    worker 要的是「处理哪几个」，糊 200 行路径进 prompt 只会挤掉真正的失败项。"""
    text = ", ".join(paths[:_MAX_LISTED_PATHS])
    if len(paths) > _MAX_LISTED_PATHS:
        text += f"（另有 {len(paths) - _MAX_LISTED_PATHS} 个）"
    return text


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

    def _archive(
        self, task_id: str, round_no: int, result: "AttemptResult"
    ) -> ArchiveResult:
        """归档这一轮的执行现场。归档不了就照旧，绝不让整轮失败。

        store.data_root 为 None 是 `:memory:` 库（测试）—— 没有落盘位置，
        直接返回空结果，调用方会退回 adapter 给的原路径。
        """
        root = getattr(self._store, "data_root", None)
        if root is None:
            return ArchiveResult()
        return archive_attempt(
            root,
            task_id,
            round_no,
            transcript_src=result.transcript_path,
            diff=result.diff,
        )

    # ---------- 预分级：不通过就一次都不派发 ----------

    def _record_blocked(
        self,
        task: Task,
        grade: Grade,
        model: str,
        *,
        check: str = "pre-dispatch-grading",
        expected: str = "class A/B (unmanned allowed)",
        got: str | None = None,
    ) -> int:
        """记一条没派发的 attempt 然后升级。**adapter 一次都不调，不花钱。**

        check / expected / got 可换：预派发拦截不止分级一种（还有悬空
        spec_ref），但它们的记账形状必须一样 —— 都要留下 attempt、都要
        NOT_DISPATCHED、都要 escalated，否则报表上会漏掉一整类拦截。
        """
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
                    "check": check,
                    "command": "",
                    "expected": expected,
                    "got": grade.reason if got is None else got,
                }
            ],
        )
        self._store.finalize(aid, Resolution.ESCALATED)
        return aid

    def _record_gate_events(self, aid: int) -> None:
        """把 adapter 上权限门累计的非 ALLOW 事件写进审计。

        用 getattr 探而不是 isinstance(ClaudeCodeAdapter)：dispatcher 只依赖
        HarnessAdapter 协议，测试里的假 adapter 和将来别的 harness 都不该被迫
        长出这个方法。没有 broker 的 adapter 直接跳过，行为跟接门之前一致。

        每轮 drain 一次并清空累计：不清的话第 2 轮会把第 1 轮的事件再写一遍，
        审计里同一个拦截出现在多个 attempt 下，看起来像 worker 反复撞墙。
        """
        drain = getattr(self._adapter, "drain_gate_events", None)
        if drain is None:
            return
        for event in drain():
            self._store.record_permission_event(aid, **event)

    def _ops(self, task: Task) -> tuple[str, ...]:
        """分级用的 declared_ops —— **在这里重扫一遍 prompt**，不信 YAML。

        为什么不能只读 task.declared_ops：guard 的关键词扫描只在
        `TaskExtractor.run` 里跑过一次（intake/extract.py），也就是只有
        `factory prd` 那条路走过。`factory queue <task.yaml>` 直接 bl.add，
        人手写的 YAML 一个字节都不过 guard。实测同一份任务：

            prompt: 「把生产库的 users 表 drop 掉再重建，然后 force push 到 main」
            declared_ops: []
            走 prd   → harden 补出 force_push → D 类，硬闸门，不派发
            走 queue → declared_ops 空 → 「no rule matched -> default A」→ 无人放行

        两条入口对同一份任务给出相反判决，差别只在 guard 有没有跑过。
        Global Constraint 10 要的是「dispatcher 里不存在绕过 D 类判定的路径」——
        绕过它的不是代码，是数据，所以补在 dispatcher 这个派发咽喉上。

        安全性：harden_ops 只增不减且幂等（实测），所以重扫只可能把分级**调严**，
        对已经走过 prd 的任务是空操作。代价是人手写 YAML 时 prompt 里提一句
        force push 就会被拦 —— 这个方向的误拒是可接受的那一侧。
        """
        ops, _ = harden_ops(task.prompt, task.declared_ops)
        return ops

    def run(self, task: Task, workspace: Path) -> DispatchReport:
        ops = self._ops(task)
        pre = self._engine.grade(task.declared_paths, ops)

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

        # 悬空 spec_ref：编号有、正文查不到。这条必须在派发**之前**拦。
        # 放它进去的话，规格监工会拿着一行 `- AC-1` 去核 diff，判 fail，
        # 而那条 claim 不带 supervisor- 前缀 → 被当成真问题打回 worker →
        # worker 改不了「AC-1 没有正文」→ 三轮烧完升级给人。实测过。
        spec = task.resolve_spec(workspace)
        if not spec.ok:
            detail = spec.doc_error or (
                f"spec_doc `{task.spec_doc}` 里查不到：{', '.join(spec.missing)}"
            )
            model = self._router.model_for(pre.oracle_class, 1)
            aid = self._record_blocked(
                task, pre, model,
                check="pre-dispatch-spec-ref",
                expected="spec_ref 的每个编号都能在 spec_doc 里查到正文",
                got=detail,
            )
            return DispatchReport(
                Outcome.ESCALATED, (aid,), 0, pre,
                f"pre-dispatch spec_ref: {detail} —— 编号不是标准，"
                f"派发只会烧三轮再上人",
            )

        return self._loop(task, Path(workspace), pre)

    # ---------- 主循环 ----------

    def _loop(self, task: Task, workspace: Path, pre: Grade) -> DispatchReport:
        attempt_ids: list[int] = []
        feedback: tuple[dict, ...] = ()
        grade = pre

        # 所有 git 类闸门的基线：**整个任务派发之前取一次，不是每轮取一次**。
        #
        # 每轮取的版本实测会漏：第一轮 worker 写了 pre-commit → 判红打回 →
        # 第二轮开头重取基线，那个 hook 已经在里面了 → 差异为空 → 合并，
        # 而 commit 照样被 hook 污染（rounds=2 的真跑抓到）。提到循环外之后，
        # worker 想过这道闸门只有一条路：把 hook 改回去。
        #
        # 十道闸门各自盯什么、为什么是这个顺序，见 factory/gates/specs.py。
        # 那里每道闸门都记着完整的攻击复现路径。
        gate_before = gates.baseline(workspace)

        # 验收标准的基线。**没有并进 gates 包**：它不是纯 git 探针，要靠
        # `task.criteria(workspace)` 解析仓库里的 spec_doc，依赖 task 本身。
        # 同样在循环外取一次，理由和上面一模一样，而且这份更隐蔽 —— 递给规格
        # 监工的 criteria 是每轮重新解析的，而那个文件 worker 写得进去。
        # 被判的东西能改判它的标准。
        #
        # 只存这个任务 spec_ref 的正文，不存整份文档：实测改文档里**别的
        # 编号**、往尾部加新条目都不该响，只有这几条自己变了才该响。
        criteria_before = task.criteria(workspace)

        # 连续「worker 起来了但不吐字节」的次数。挂死是**环境**故障（CLI 卡在
        # ep_poll、零网络连接），不是模型能力不够 —— 拿 haiku→sonnet→opus 的
        # 阶梯去重试它，只是把同一个环境问题烧三遍。实测一次派发三轮里两轮
        # 这么没了，总墙钟 2276s，真正的产出来自唯一没挂的那一轮。
        stall_streak = 0

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
            # 归档执行现场，**在落库之前**：库里要存的是归档后的稳定路径，
            # 不是 adapter 给的 /tmp 路径。顺序反了就等于没修 —— 字段指着
            # 一个随时会被 systemd-tmpfiles 清掉的目录。
            archived = self._archive(task.task_id, round_no, result)

            self._store.record_result(
                aid,
                diff_hash=result.diff_hash,
                commit=None,
                transcript_path=archived.transcript_path or result.transcript_path,
                diff_path=archived.diff_path,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
                wall_clock_ms=result.wall_clock_ms,
                harness_version=result.harness_version,
            )

            # 权限门的事件落库。**紧跟 record_result**，不放在轮次末尾：
            # 中间任何一个 return（挂死熔断、后分级硬闸门、监工判失败）都会
            # 跳过后面的代码，那时这一轮被拦下的动作就永远进不了审计 ——
            # 而恰恰是「被拦了所以任务失败」那几轮最需要留证据。
            self._record_gate_events(aid)

            # 挂死熔断。和下面 merged.faults 那条同构：「重试也是坏的，当场
            # 上人，不浪费剩余轮次」—— 只是坏的那一侧从监工换成了执行环境。
            #
            # 放在监工之前：worker 一个字节都没吐，工作区必然没有产出，
            # 跑一遍回归监工只是白花几分钟去确认「没改动」。
            #
            # 阈值 2 而不是 1：单次挂死可能是中转站抖一下，值得再试一次
            # （实测里 attempt1 就是重试后跑通的）。连着两次就不是抖动了。
            if result.stalled:
                stall_streak += 1
                if stall_streak >= _STALL_STREAK_LIMIT:
                    self._store.finalize(aid, Resolution.ESCALATED)
                    return DispatchReport(
                        Outcome.ESCALATED,
                        tuple(attempt_ids),
                        round_no,
                        grade,
                        f"worker 连续 {stall_streak} 次挂死（起来了但不产出），"
                        f"判为执行环境故障，不再往上换模型：\n{result.error_text}",
                    )
            else:
                # 只要有一轮正常产出就清零 —— 连续性才是环境故障的信号，
                # 累计次数不是。
                stall_streak = 0

            # 后分级（= 风险监工）：用真实改动的文件再判一次
            # 用 _ops 而不是 task.declared_ops：后分级和预分级必须同一份输入，
            # 否则「prompt 里写了 force push」这条在后分级又消失了。
            post = self._engine.grade(result.changed_paths, self._ops(task))
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

            reports = self._review(
                task, workspace, result, gate_before, criteria_before,
            )
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
                # 全绿了才落地。中途打回的轮次刻意不提交 —— capture_diff 用的是
                # `git diff HEAD`，中途提交会让下一轮的 diff 变成「相对上一轮的
                # 增量」而不是「这个任务改了什么」，审计里 diff_hash 的含义会在
                # 多轮任务上悄悄换掉。理由全文见 harness/landing.py 的 docstring。
                #
                # 落地失败**不改判决**：后果只是 commit 字段仍为 None，
                # 也就是退回这个功能存在之前的状态。让一个已经全绿的任务因为
                # user.email 没配变成 escalated，是拿真问题换假问题。
                # paths 传的是 capture_diff 看到的那一组 —— 提交的必须正好是
                # 监工审过的。真跑里 `add -A` 把 check 自己生成的 .pyc 也提交
                # 了，于是 commit 和 diff_hash 开始描述不同的东西。
                landed = land(workspace, task_id=task.task_id,
                              attempt_no=round_no,
                              paths=result.changed_paths)
                if landed:
                    self._store.record_commit(aid, landed.commit)
                self._store.finalize(aid, Resolution.MERGED)
                return DispatchReport(
                    Outcome.MERGED, tuple(attempt_ids), round_no, grade,
                    commit=landed.commit, landing_note=landed.reason,
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
            # BEACON 不是闸门，是「金丝雀到底跑过没有」的可见性记录（§9.1）。
            # 它的 claims 必须落库（不然盲区不可见），但绝不能进 hard/soft ——
            # 进 hard 会让所有非标测试目录（spec/、t/）的仓库一律拦停，
            # 那是把审计完整性问题换成整类仓库无法出货。
            if r.role == SupervisorRole.BEACON:
                continue
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

    def _review(
        self, task, workspace, result,
        gate_before: gates.Snapshot,
        criteria_before: tuple[str, ...],
    ) -> tuple[SupervisorReport, ...]:
        """harness 报错 / 空 diff 都算这一轮红，且不跑 check、不调模型监工。

        原因：check 全绿但 agent 什么都没改，说明 check 太弱或任务已完成，
        两种都需要人看一眼，不能静默 merge。没有 diff 时调模型监工纯烧钱。

        两个基线参数刻意**都没有默认值**，判据必须是「这一轮变了什么」，而
        拿不到「之前是什么」的时候要炸，不要猜：

          `gate_before` 必须是 `_loop` 循环**外**那次 `gates.baseline()` 的
            返回值。缺 key 会在 `gates.evaluate` 里当场 KeyError。随手填
            `{}` 或空集合的后果分两种，都很坏：hooks 那道会让本来就装了合法
            pre-commit 的仓库每个任务判红；head-moved 那道填 `("", "")` 会让
            **每个**任务判红（空哈希 vs 真哈希恒不等）。恒红和恒绿一样坏，
            但恒红会被人当成闸门有毛病，比恒绿更快被拆掉。
          `criteria_before` 填 `()` 会让每个引 spec_doc 的任务判红
            （空基线 vs 有标准 = 差异）。
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

        # git hooks 被动过。**这一条放在三道检测的最前面**，因为只有它的
        # 影响范围在这个 workspace 之外：`.git/hooks` 属于**公共** git dir，
        # 父仓库和所有并行 worktree 共用一份（实测 hooks_dir 在两边返回同一
        # 个路径）。sandbox 又刻意把 git_dir 放进可写（capture_diff 要写
        # index.lock，见 sandbox.git_dir），于是 worker 写得进去。
        #
        # 实测的后果：pre-commit 里 `echo EVIL >> a.py; git add a.py`，
        # 监工审过的是 'x\ny\n'，`git show HEAD:a.py` 是 'x\ny\nEVIL=1\n'。
        # 也就是 landing 模块 docstring 里那句「监工审的是前者，出货的是
        # 后者，中间那段差额没有任何人看过」，只是这次差额是 hook 加的。
        #
        # 修法不是给 land 加 `--no-verify`：那会把人自己装的 lint hook 也
        # 一起关掉（landing.py 第 27 行是刻意的）。要区分的是「仓库本来
        # 有 hook」和「这一轮 worker 动了 hook」，所以判据是指纹差异。
        # 十道 git 类闸门，按 gates.ORDER 的顺序过。第一道拦下的就返回。
        #
        # 顺序是实测教训不是排版（hooks 最前因为它影响 workspace 之外；
        # git-config 必须在 diff-suppressed 之前，否则 core.attributesFile
        # 能让后者的基线和现状都变成「已关掉」→ 差异恒空 → 被绕过）。
        # 每道闸门盯什么、怎么被实测攻破过，见 factory/gates/specs.py。
        #
        # 基线来自 `_loop` 循环**外**那次 gates.baseline()。这里不重取 ——
        # 重取就是那个被真跑抓到的漏洞：worker 第一轮种 hook、判红打回、
        # 第二轮基线里已经有它了、差异为空、合并出货。
        if breach := gates.evaluate(
            workspace, gate_before, changed_paths=result.changed_paths
        ):
            return (
                self._blocked(
                    breach.name, breach.oracle, breach.want, breach.got
                ),
            )

        if criteria_before != (criteria_now := task.criteria(workspace)):
            return (
                self._blocked(
                    "spec-criteria-mutated",
                    f"派发前解析 spec_ref 得到 {len(criteria_before)} 条验收标准",
                    "worker 没有改动它自己被核对的那几条标准",
                    f"这一轮之后重新解析得到 {len(criteria_now)} 条，"
                    f"派发时这几条已经不在了："
                    f"{_listed(_criteria_delta(criteria_before, criteria_now))}。"
                    f"改标准不是完成任务，规格监工核的必须是派发时那份",
                ),
            )

        # 读取 checks：优先任务 YAML，为空则收 worker 自己写的 .checks.json
        # （契约在 _prompt 里发给它，见 factory/checks_contract.py）
        checks = task.checks or load_worker_checks(workspace)

        # 永真 check 当没有 check。worker 被要求「留下判据」之后，最省力的
        # 满足方式是写一条 `echo ok` —— 它让回归监工绿，而绿的含义是空的。
        # 这比没有 check 更坏：no-checks-defined 会打回，伪造的绿会 merge。
        #
        # 只在 worker 自写的那组上查。任务 YAML 里的 check 是人写的，人有权
        # 写一条看起来无聊的命令（`true` 当占位、跑一个外部脚本），
        # 替人否决他自己定的判据不是这一层该做的事。
        if not task.checks and checks:
            if vacuous := vacuous_checks(checks):
                return (
                    self._blocked(
                        "vacuous-checks",
                        "; ".join(c.command for c in vacuous),
                        "能失败的判据",
                        f"这 {len(vacuous)} 条 check 恒为真，撤销本次改动它们照样绿，"
                        f"等于没有验收：{_listed(tuple(c.name for c in vacuous))}。"
                        f"换成真正能验证改动的命令",
                    ),
                )

        checks = checks + self._runbook_checks(workspace, result)
        regression = self._supervisor.review(workspace, checks)

        # 金丝雀：验一次那份绿还能不能被推翻（塞一条必然失败的测试进去，
        # check 应该变红）。触发条件有两条，任一命中就跑。
        #
        # 【条件二·人写来源】这一轮动了「能改变裁决」的文件
        # （conftest.py / pyproject.toml 之类）。以下这段讲的是这一支：
        #
        # 为什么另立一条而不是扩 runner_hooks：那条判的是「**新增**了 runner
        # 自动加载的文件」，而伪造绿不需要新增。实测三条都拿到退出码 0 ——
        # 改已存在的 tests/conftest.py 追加 makereport hookwrapper、
        # 同一个 conftest 里改 session.exitstatus、pyproject.toml 的
        # `addopts = "-p myplug"` 加载本地插件。前两条 runner_hooks 报不出来，
        # 第三条的文件名还在它的刻意排除名单里（任务合法要改 pyproject）。
        #
        # 只在回归监工 PASS 之后验：红的绿不用验，省一次探针。
        # 只在这类文件被改动时验：本仓库 55 次提交里 conftest.py 和
        # pyproject.toml 各改过 2 次（约 7%），所以绝大多数轮次不多花这一次。
        # 而合法改动的金丝雀是红的（实测：加夹具、加 marker 都是非 0），
        # 所以这 7% 也不会变成误拒。
        # 【条件一·worker 来源】worker 自写的 check 无条件全验，
        # 不看改了哪些文件、不抽样。
        #
        # 上面那段抽样逻辑（7% 的轮次）成立的前提是「伪造绿需要改判决文件」。
        # 对人写的 check 是对的：命令是人定的，worker 只能从裁决装置下手。
        # 但 worker 自己写 check 时根本不用碰 conftest —— 新加一个
        # `def test_ok(): assert True` 就够了，语法层的 vacuous_checks 看
        # 命令（`pytest tests/`）完全正常，判决文件一个没动，于是探针不跑，
        # 恒真自测一路绿到 merge。
        #
        # 所以触发条件按 check 的来源分岔：worker 来源全跑（这一组本来就是
        # 它自己定的判据，可信度最低，且只有一两条，成本可接受），
        # 人写来源保持原来的判决文件抽样。
        worker_authored = not task.checks
        beacon: SupervisorReport | None = None
        touched_judges = judge_files_touched(result.changed_paths)
        if regression.verdict is Verdict.PASS and (
            worker_authored or touched_judges
        ):
            probe_skips: list[tuple[str, str]] = []
            for spec in checks:
                res = probe(Path(workspace), spec.command,
                            timeout_s=spec.timeout_s)
                if res.fake_green:
                    return (
                        self._blocked(
                            "fake-green",
                            spec.command,
                            "塞进一条必然失败的测试之后 check 变红",
                            # 触发原因决定这句怎么说。worker 来源时
                            # touched_judges 是空的，照原样拼会打出「这一轮
                            # 改过：」后面跟一片空白 —— 那种提示比不给更糟。
                            f"{res.reason}。"
                            + (
                                "这条 check 是 worker 自己写的，所以全量验"
                                if worker_authored
                                else f"这一轮改过：{_listed(touched_judges)}"
                            ),
                        ),
                    )
                if res.skipped:
                    probe_skips.append((spec.command, res.reason))

            # §9.1：探针没跑成必须在审计里留痕。走 reports 路径（下面的 append），
            # 由调用方那个 record_verdict 循环统一落库 —— 直写要重复实现落库、
            # tokens、cost 三件事。
            #
            # 为什么不拦：skipped 的主因是「测试目录不叫 tests/」这类仓库约定
            # 差异（spec/、t/ 都常见），拦住等于让整类仓库无法出货 —— 拿可用性
            # 问题换停摆问题。探针本身也会因自己超时而 skip，那时任务其实是全绿的。
            # 不拦的代价写在 claims 里，由报表兜。
            #
            # 为什么必须记：不记的话审计里「验过是真的」和「根本没验」完全同形，
            # 而后者是伪造绿的可乘之机。
            if probe_skips:
                beacon = SupervisorReport(
                    role=SupervisorRole.BEACON,
                    verdict=Verdict.PASS,
                    claims=tuple(
                        {
                            "check": "probe-skipped",
                            "command": cmd,
                            "expected": "金丝雀能落地并跑一次",
                            "actual": reason,
                            "note": "这一轮的绿没有被反向验证过",
                        }
                        for cmd, reason in probe_skips
                    ),
                )

        reports = [
            regression,
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
                    # criteria = spec_doc 里解析出的**正文** + acceptance。
                    # 递编号（`AC-1`）等于没递标准 —— 见 factory/spec_doc.py。
                    # 走到这里 resolve 一定成功过：run() 里拦过一道。
                    criteria=task.criteria(workspace),
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
        # BEACON 最后追加：它不参与任何 withheld 计算（上面两处用 reports[0]），
        # 位置靠后也让 `factory show` 的裁决列表把可见性记录排在闸门后面。
        if beacon is not None:
            reports.append(beacon)
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
        """派给 worker 的 prompt。

        任务自带 checks 时**不**附契约：判据已经有了，再要 worker 写一份
        `.checks.json` 只会让它去猜一组和 YAML 里那组竞争的判据。
        契约只在「没人给判据，得由 worker 自己留」时才是必要信息。

        没有 checks 时必须附 —— 读取端一直在等 `.checks.json`，但在此之前
        没有任何一处告诉过 worker 这个文件的存在，于是它必然三轮全红在
        no-checks-defined 上（audit.db 里两条实测轨迹）。见 checks_contract。
        """
        extra: list[str] = []
        if not task.checks:
            extra.append(contract_text())
        if feedback:
            extra.append(
                "上一轮被监工打回。以下是具体失败项，逐条修掉，不要改动无关文件：\n"
                f"{self._render(feedback)}"
            )
        # 没有附加段时原样返回 task.prompt，连尾换行都不加：worker 收到的
        # 第一轮 prompt 应当逐字等于人写的那句（test_first_round_prompt_is_untouched）。
        if not extra:
            return task.prompt
        return "\n\n".join([task.prompt, *extra]) + "\n"
