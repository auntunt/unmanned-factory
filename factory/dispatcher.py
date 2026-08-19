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
from factory.harness.landing import land
from factory.routing import Router
from factory.harness.verdict_probe import judge_files_touched, probe
from factory.harness.workspace import (
    changed_config,
    changed_hooks,
    diff_suppressed,
    git_config,
    head_position,
    hook_fingerprint,
    info_attributes,
    index_skipped,
    newly_skipped,
    neighbour_context,
    replace_refs,
    runner_hooks,
    shadow_code,
    staged_gitlinks,
)
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


def _head_desc(pos: tuple[str, str]) -> str:
    """(哈希, ref) 转成给 worker 看的一行。

    哈希截到 12 位：递进 prompt 里要的是「动过没动过」和「往哪动了」，40 位
    全写进去只是占字数。ref 为空串是 detached，明写出来 —— 「从分支变成
    detached」本身就是这道闸门要报的一种移动，只印哈希会让它读起来像没变。
    """
    commit, ref = pos
    short = commit[:12] or "（无提交）"
    return f"{short} @ {ref or 'detached HEAD'}"


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

        # git hooks 的基线：**整个任务派发之前取一次，不是每轮取一次**。
        # 每轮取的版本实测会漏：第一轮 worker 写了 pre-commit → 判红打回 →
        # 第二轮开头重取基线，那个 hook 已经在里面了 → 差异为空 → 合并，
        # 而 commit 照样被 hook 污染（rounds=2 的真跑抓到）。
        # 提到循环外之后，worker 想过这道闸门只有一条路：把 hook 改回去。
        hooks_before = hook_fingerprint(workspace)

        # 验收标准的基线。同样**在循环外取一次** —— 理由和 hooks 一模一样，
        # 而且这份更隐蔽：递给规格监工的 criteria 是每轮重新从仓库里那份
        # spec_doc 解析出来的（_review 里 `task.criteria(workspace)`），而
        # 那个文件 worker 写得进去。被判的东西能改判它的标准。
        #
        # 只存这个任务 spec_ref 的正文，不存整份文档：实测改文档里**别的
        # 编号**、往尾部加新条目都不该响，只有这几条自己变了才该响。
        criteria_before = task.criteria(workspace)

        # 索引跳过标记的基线。同样在循环外 —— 挂上标记这个动作只发生一次，
        # 之后每一轮它都还在，每轮重取基线的版本从第二轮起就看不见了。
        skipped_before = index_skipped(workspace)

        # 本地 git config 的基线。也在循环外，理由同上。
        # 这份是四个基线里覆盖面最宽的一个：config 里的开关能直接改变 git
        # **自己**回答问题的方式，而它们不出现在任何 diff 里。
        config_before = git_config(workspace)

        # `.git/info/attributes` 的基线。设 attributes 一共三条路，这是第三条：
        #   tree 里的 .gitattributes  → diff_suppressed（第七个洞，--source=HEAD 做基线）
        #   core.attributesFile      → changed_config（第十三个洞）
        #   .git/info/attributes     → 这里
        # 前两条堵上之后这条还通：实测写一行 `*.py -diff` 进去，diff 正文退化成
        # 「Binary files differ」，而 diff_suppressed 差异为空 —— `check-attr
        # --source=HEAD` 也读这个文件，基线和现状都是「已关掉」。它又不是 config
        # 键，第十三道闸门也看不见；文件本身不在 diff、不在 git status 里。
        #
        # 和 hooks 同一个性质：`.git/info` 属于 **common dir**，父仓库和所有并行
        # worktree 共用一份（实测从 worktree 写进去，主仓库的 diff 也瞎了），
        # 所以一个 worker 能污染所有并行任务的监工。
        attrs_before = info_attributes(workspace)

        # HEAD 的位置。前五个基线盯的都是 `git diff HEAD` 的**输入**（hooks、
        # 属性、config、索引标记、标准），这一个盯的是它对比的那个**点**。
        #
        # 也在循环外，但这里的理由和前面几个不同：一轮之内**没有任何合法动作
        # 会移动 HEAD**。land 是全流程唯一的提交点（landing.py:131），它跑在
        # 这道闸门之后（_review 在 368 行，land 在 398 行），而被打回的轮次
        # 刻意不提交。所以基线取一次就永远对得上，不存在「合法漂移」。
        head_before = head_position(workspace)

        # replace ref 的基线。这一条比前面所有的都靠下：前面盯的是 `git diff
        # HEAD` 的输入和基线端，这一条盯的是 git **回答对象内容时说的话**。
        #
        # 实测替换 HEAD 那个 commit 对象，一个**全新文件** evil.py 同时从 diff
        # 和 changed_paths 里消失（不替换时两处都有它）。全新非 ignore 文件的
        # 视野只有一处 —— `capture_diff` 里那句 `add -A -N`，替换掉的正是它，
        # 此处没有第二道防线（`shadow_code` 管的是被 gitignore 挡住的文件，
        # 替换与否它都返回 ()，别把它当兜底）。
        #
        # 也在循环外：一轮之内没有任何合法动作会设 replace ref（capture_diff
        # 和 land 都不碰它，本仓库一条都没有）。
        replaces_before = replace_refs(workspace)

        # gitlink 基线。前面所有攻击改的都是 git 在某件事上「怎么回答」，这条
        # 不改回答，它**把代码藏进一个 git 不展开的黑盒**。
        #
        # worker 在工作区里 `git init evil/`，往里提交任意代码，再 `git add evil`。
        # git 如实地把 evil 记成一条 gitlink（mode 160000）——索引里只有一行
        # 「evil → 某提交哈希」。diff 和 changed_paths 都只看见目录名，里面的
        # 任何一行代码对所有外层查询都隐形。
        #
        # 实测十二道现有闸门全部静默（changed_paths=('evil',)、diff 为空、
        # runner_hooks/shadow_code/replace_refs 全 ()）。而 check 跑在真实文件树
        # 上：pytest 在 --collect-only 阶段就会执行 evil/conftest.py，哨兵文件被
        # 写入，多次独立量测一致。危害性质同 replace ref：伪造那份绿，不出货。
        #
        # 判「这一轮多出来的」而非「有没有」：真实项目合法拥有子模块，子模块在
        # 索引里也是 mode 160000。本仓库一条都没有，但不应写死这个假设。
        gitlinks_before = staged_gitlinks(workspace)

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
                task, workspace, result, hooks_before, criteria_before,
                skipped_before, config_before, attrs_before, head_before,
                replaces_before, gitlinks_before,
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
        hooks_before: frozenset[tuple[str, str]],
        criteria_before: tuple[str, ...],
        skipped_before: frozenset[str],
        config_before: frozenset[tuple[str, str]],
        attrs_before: str,
        head_before: tuple[str, str],
        replaces_before: frozenset[str],
        gitlinks_before: frozenset[str],
    ) -> tuple[SupervisorReport, ...]:
        """harness 报错 / 空 diff 都算这一轮红，且不跑 check、不调模型监工。

        原因：check 全绿但 agent 什么都没改，说明 check 太弱或任务已完成，
        两种都需要人看一眼，不能静默 merge。没有 diff 时调模型监工纯烧钱。

        `hooks_before` 刻意**没有默认值**：给它填 `frozenset()` 会让漏传的
        调用方在一个本来就装了合法 pre-commit 的仓库上每个任务都判红
        （空基线 vs 有 hook = 差异）。让它当场 TypeError 好过每次都响。

        `criteria_before` 同理没有默认值：填 `()` 会让漏传的调用方在每个
        引 spec_doc 的任务上都判红（空基线 vs 有标准 = 差异）。

        `skipped_before` / `config_before` / `attrs_before` / `head_before` /
        `replaces_before` / `gitlinks_before` 也一样，一个默认值都没有。这八个
        基线是同一个教训的八份：判据必须是「这一轮变了什么」，而拿不到「之前是
        什么」的时候要炸，不要猜。

        `head_before` 这一份尤其不能给默认值：随手填 `("", "")` 不会像别的
        基线那样「每次都响」，它会让这道闸门在**每个**任务上都判红（空哈希 vs
        真哈希恒不等），于是最省事的修法就是把闸门关掉。恒红和恒绿一样是坏的，
        但恒红会被人当成闸门有毛病，比恒绿更快被拆。
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
        if touched := changed_hooks(hooks_before, hook_fingerprint(workspace)):
            return (
                self._blocked(
                    "git-hook-touched",
                    "sha256 of executable files in $(git rev-parse --git-common-dir)/hooks",
                    "这一轮没有改动 git hooks",
                    f"{len(touched)} 个 hook 变了：{_listed(touched)}。hooks 目录"
                    f"被父仓库和所有并行 worktree 共用，且 commit 时会执行",
                ),
            )

        # 影子代码：被 .gitignore 挡住、四道闸门都看不见的代码文件。
        # **必须在跑 check 之前拦**，因为它污染的正是那份绿 —— check 跑在真实
        # 文件树上，会 import 并执行这些没人审过的文件（实测执行到了）。
        # 拦下来之后跑 check 只是在一个已知被污染的树上多花一次钱。
        #
        # 不挂进范围监工：那个监工 declared_paths 为空时 PASS，而口述来源的
        # 任务绝大多数为空。影子代码不依赖任何声明才有意义。
        if shadow := shadow_code(Path(workspace)):
            return (
                self._blocked(
                    "shadow-code",
                    "git ls-files --others --ignored --exclude-standard",
                    "没有被 .gitignore 挡住的新代码文件",
                    f"{len(shadow)} 个文件不在任何闸门视野里，但 check 会执行"
                    f"它们：{_listed(shadow)}",
                ),
            )

        # runner 自己会去加载的配置文件（conftest.py 等），这一轮新增的。
        # 同样必须在跑 check 之前 —— 这类文件的作用就是改变 check 的行为。
        # 实测：一个必然失败的测试 + 一个清空收集的 conftest.py
        # → `pytest -q` 退出码 0。见 workspace.runner_hooks。
        #
        # 和影子代码分成两条 claim 而不是合并：worker 要做的事不一样。
        # 影子代码是「把这个文件挪进版本控制或删掉」，runner hook 是
        # 「别自己出卷子」。糊成一条会让打回的指令变模糊，白烧一轮。
        if hooks := runner_hooks(Path(workspace)):
            return (
                self._blocked(
                    "runner-hook-added",
                    "git diff HEAD --name-only --diff-filter=A",
                    "这一轮没有新增 runner 会自动加载的配置文件",
                    f"新增了 {len(hooks)} 个 runner 自动加载的文件，它们能直接"
                    f"改变 check 的结果：{_listed(hooks)}",
                ),
            )

        # diff 正文被 .gitattributes 关掉的路径。放在 check 之前 —— 它伤的
        # 不是那份绿（check 照跑照红），是两个**看 diff 正文**的模型监工的
        # 输入。一行 `*.py -diff` 就让 `git diff HEAD` 里的 a.py 退化成
        # 「Binary files a/a.py and b/a.py differ」，实测 `"pdb.set_trace"
        # in diff` 变成 False，而 changed_paths 完全不受影响 —— 范围监工和
        # runbook 照常工作。三道闸门里两道行为正常，是这个洞难被发现的原因。
        #
        # 不改成 `git diff --text` 硬穿：实测一个 5120 字节的 PNG 会摊成 28
        # 行原始字节，真有大二进制文件时会把监工的 prompt 冲掉。所以是定点
        # 检测，不是全局开关。
        #
        # 判据是「这一轮才被关掉」而不是「有没有被关」：本仓库有 uv.lock，
        # 一个合法的 `*.lock -diff` 会让每次改锁文件的任务都判红。基线取自
        # `git check-attr --source=HEAD`，不需要提前快照。
        if muted := diff_suppressed(Path(workspace), tuple(result.changed_paths)):
            return (
                self._blocked(
                    "diff-suppressed",
                    "git check-attr diff（对比 --source=HEAD）",
                    "改动的源码文件在 diff 里能看到正文",
                    f"{len(muted)} 个路径这一轮被 .gitattributes 关掉了 diff，"
                    f"监工看不到改了什么：{_listed(muted)}",
                ),
            )

        # 索引里这一轮新挂上 assume-unchanged / skip-worktree 的路径。
        #
        # 必须在跑 check **之前**：它伤的正是那份绿。实测的完整攻击 ——
        # 可见地把 `add` 改成 `a - b`（一个监工看得见的错），同时把
        # tests/test_calc.py 改成 `assert True` 并挂上 --assume-unchanged：
        #   监工看到的 changed_paths = ('calc.py',)   ← 测试文件不在里面
        #   四道闸门 shadow/hooks/muted 全空
        #   `pytest -q` 退出码 0，"1 passed"
        #
        # 比第七个洞（.gitattributes）更彻底：那个只让 diff 正文退化，路径还在
        # changed_paths 里，范围监工和 runbook 照常工作。这个把路径本身抹掉，
        # 六道闸门里除了这一道全盲。
        #
        # 也不是金丝雀那条能覆盖的：金丝雀判「这一轮动了能改变裁决的文件」，
        # 而这里那个文件在 git 眼里根本没动 —— capture_diff 的
        # `git add -A -N` 不碰它，`git diff HEAD --name-only` 里没有它。
        #
        # 未审代码不会因此出货：实测 `git add -A` 不 stage 这类文件，land
        # 那步会以「无文件要提交」失败。所以这道闸门保的是那份绿的可信度，
        # 不是出货物的纯净度。
        if skipped := newly_skipped(skipped_before, index_skipped(workspace)):
            return (
                self._blocked(
                    "index-skip-flag",
                    "git ls-files -v 首字母全是 H",
                    "这一轮没有给任何路径挂上 assume-unchanged / skip-worktree",
                    f"{len(skipped)} 个路径被挂上了索引跳过标记，git 从此不看它们"
                    f"的改动，而 check 跑在真实文件树上：{_listed(skipped)}",
                ),
            )

        # 本地 git config 被动过。**这一条必须在两道 diff 相关闸门之前**，因为
        # 它能让那两道自己失效：
        #
        #   diff.external=<脚本>     → `git diff HEAD` 返回空字符串（实测 len=0）。
        #                             而 `--name-only` 不走 external，changed_paths
        #                             照常非空 → 上面那道「空 diff」判定也不响。
        #                             两个模型监工收到的是 "(空 diff)"。
        #   core.attributesFile=<外部> → 效果同第七个洞（`*.py -diff`），但
        #                             `check-attr --source=HEAD` **也读这个设置**，
        #                             基线和现状都是「被关掉」→ 差异为空 →
        #                             diff_suppressed 那道闸门被绕过（实测）。
        #
        # 判「这一轮变了」而不是查开关名单：git 的 config 键有几百个，逐个堵
        # 是猜机制。而且 config 里的东西**不出现在任何 diff 里** —— 四道闸门
        # 加上范围监工全都看不见它，只有拿基线比才看得见。
        #
        # 合法改动不会响：实测 capture_diff / land 都不写 --local（landing.py
        # 只 `git config user.email` 读一次，身份是瞬时 `-c` 传的），本仓库
        # 6 个键在一整轮里恒定。
        if cfg := changed_config(config_before, git_config(workspace)):
            return (
                self._blocked(
                    "git-config-touched",
                    "git config --local --list（派发前后对比）",
                    "这一轮没有改动仓库本地 git config",
                    f"{len(cfg)} 个 config 键变了：{_listed(cfg)}。config 不出现在"
                    f"任何 diff 里，而其中若干键能直接改变监工看到的 diff",
                ),
            )

        # `.git/info/attributes` 这一轮变了。设 attributes 一共三条路，这是第三条：
        #   tree 里的 .gitattributes  —— 第七道闸门（diff_suppressed）管
        #   core.attributesFile       —— 上面那道 git-config-touched 管
        #   .git/info/attributes      —— 这一条，两道都管不到
        #
        # 为什么第七道管不到：它的基线是 `check-attr --source=HEAD`，而
        # `--source` 只换「tree 里那份 .gitattributes」，info/attributes 属于
        # 仓库级，**基线侧和现状侧都会读它** → 差异恒为空。实测写一行
        # `*.py -diff` 之后 diff_suppressed 返回 ()，而 diff 正文已经退化成
        # 「Binary files a/a.py and b/a.py differ」。
        #
        # 危害面比第七个洞大：`.git/info` 在 **common dir**，父仓库和所有并行
        # worktree 共用一份（实测从 worktree 写进去，主仓库的 diff 也瞎了）——
        # 和 hooks 同一个性质，一个 worker 能污染所有并行任务的监工。
        #
        # 判「这一轮变了」而不是「有没有这个文件」：外部仓库合法拥有它是正常的
        # （常见配法是给生成物加 `-diff`）。本仓库根本没有这个文件，基线是空串。
        if info_attributes(workspace) != attrs_before:
            return (
                self._blocked(
                    "info-attributes-touched",
                    "sha256 of $(git rev-parse --git-common-dir)/info/attributes",
                    "这一轮没有改动 .git/info/attributes",
                    ".git/info/attributes 变了。一行 `*.py -diff` 就让监工看到的 "
                    "diff 正文退化成「Binary files … differ」，而这个文件不在任何 "
                    "diff、任何 config、git status 里，且被所有并行 worktree 共用",
                ),
            )

        # HEAD 这一轮被移动了。这是 `git diff HEAD` 的**基线端**被动了手 ——
        # 前面几道闸门盯的都是这条命令的输入，这道盯它对比的那个点。
        #
        # 实测：worker 自己 `git commit` 把后门提交掉，只留一处干净改动没提交。
        # 于是 `git diff HEAD` 变成「相对 worker 那次提交的增量」，监工看到的
        # paths 只有 ('README.md',)，`"pdb.set_trace" in diff` 是 False，而
        # 后门在 HEAD 里。前面五道闸门全静默（shadow=() hooks=() muted=()
        # skip=frozenset() cfg 变=False attrs 变=False），git status 只报
        # ' A README.md'。
        #
        # **这条路和第十二个洞的区别是它真的出货**：索引跳过标记那条路上
        # `git add -A` 不 stage 被标记的文件，land 会以「无文件要提交」失败，
        # 未审代码进不了历史；这条路上代码已经在历史里了，land 只是往上叠一层。
        #
        # 判 (哈希, ref) 而不是只判哈希，因为只切分支那一支哈希不变：实测
        # `git checkout -b worker-side` 之后 diff 照样诚实（pdb 可见），但 land
        # 的提交落到 `refs/heads/worker-side`，原分支 `refs/heads/feat` 看不到
        # 它 —— 人审的是原分支，货在别处。
        if (head_now := head_position(workspace)) != head_before:
            return (
                self._blocked(
                    "head-moved",
                    f"派发前 HEAD 在 {_head_desc(head_before)}",
                    "worker 没有移动 HEAD（一轮之内没有合法动作会移动它）",
                    f"HEAD 这一轮被移到了 {_head_desc(head_now)}。`git diff HEAD` "
                    "的基线是 HEAD，移动它就把监工的视野截短成「相对 worker 那次"
                    "提交的增量」，已提交的部分谁都看不见，而 land 会直接叠在上面"
                    "出货",
                ),
            )

        # `refs/replace/*` 这一轮变了。这道比前面所有闸门都更靠下：前面盯的是
        # `git diff HEAD` 的输入和基线端，这道盯的是 git **回答对象内容时说的
        # 话**。所有闸门都通过问 git 来看世界，让 git 撒谎就一次性绕过全部。
        #
        # 实测两种形态，八道闸门全静默、HEAD 哈希不变、status 只报干净那处：
        #   替换 blob   —— `git diff HEAD` 退化成空字符串，paths 为 ()。
        #   替换 commit —— 伪造 tree 里带 evil.py 的提交顶掉 HEAD，那个**全新
        #                  文件**不在 diff、也不在 changed_paths（不替换时两处
        #                  都有它，实测）。这类文件的唯一视野是 `capture_diff`
        #                  里那句 `add -A -N`，没有第二道防线。
        #
        # 危害是伪造那份绿，不是出货：land 只提交监工审过的那一组，实测 linked
        # worktree 里真跑一遍 evil.py 没进历史。但 check 跑在真实文件树上（实测
        # 能读到它），那份绿是假的。和第十二个洞同性质。
        #
        # 判「这一轮变了」而不是「有没有 replace ref」：外部仓库合法拥有它是
        # 正常的（`--convert-graft-file` 的产物就是它）。本仓库一条都没有。
        if replace_refs(workspace) != replaces_before:
            return (
                self._blocked(
                    "replace-refs-changed",
                    f"派发前 refs/replace/ 下有 {len(replaces_before)} 条",
                    "worker 没有增删改 refs/replace/ 下的引用",
                    "refs/replace/ 这一轮变了。一条 replace ref 就让 git 在「那个"
                    "对象是什么」上撒谎：替换 blob 会让 diff 变成空字符串，替换 "
                    "commit 连全新文件都能藏掉，而 check 照样在真实文件树上执行"
                    "它。这些引用不在任何 diff、任何 config、git status 里，且被"
                    "所有并行 worktree 共用",
                ),
            )

        # 这一轮多出来了 gitlink（mode 160000 的索引条目）。
        #
        # 前面所有攻击改的都是 git 「怎么回答」，这条不改回答：它**把代码藏进一
        # 个 git 不展开的黑盒**。worker 在工作区里 `git init evil/`，在里面提交
        # conftest.py / payload.py，再 `git add evil`。git 如实记一条 gitlink——
        # 索引里只有「evil → 提交哈希」，diff 和 changed_paths 都只有目录名。
        #
        # 实测十二道现有闸门全静默，同时 pytest 在 --collect-only 阶段就执行
        # evil/conftest.py（哨兵文件被写入，多次独立量测一致）。危害同 replace
        # ref：伪造那份绿，不出货（land 只提交审过的路径集合）。
        #
        # 判「这一轮多出来」而非「有没有」：真实项目合法拥有子模块，子模块在
        # 索引里也是 mode 160000。取 (路径, 哈希) 对以覆盖「路径不变、内层再
        # 提交一版」的重新指向攻击 —— 只看路径那一版对这支是盲的。
        if staged_gitlinks(workspace) != gitlinks_before:
            new = staged_gitlinks(workspace) - gitlinks_before
            return (
                self._blocked(
                    "gitlink-added",
                    f"派发前有 {len(gitlinks_before)} 条 gitlink",
                    "worker 没有新增或修改 mode-160000 的索引条目",
                    f"这一轮新增了 {len(new)} 条 gitlink（mode 160000）。gitlink "
                    "在外层 diff 里只露出一个目录名，里面可以有任意多个文件——"
                    "pytest 在 collect 阶段就会执行其中的 conftest.py，而所有"
                    "现有闸门对内层代码完全盲。这类索引条目不属于任何合法的单任"
                    "务改动；添加子模块应该走独立的、人工审核的流程",
                ),
            )

        # 验收标准这一轮被改了。判的是「被审对象改了审它的标准」。
        #
        # 路径：`_review` 里 `task.criteria(workspace)` 每轮重新解析仓库里的
        # spec_doc，而 sandbox 允许 worker 写这个文件。实测两个方向：
        #   改正文  —— `- AC-1: 返回和` 改成 `- AC-1: 返回差，已由上游确认满足`，
        #             监工拿着改后的标准核 diff，判 pass。
        #   删条目  —— 两条删成一条，`resolve_spec().ok` 变 False，但这里
        #             **不看 ok**，criteria 静默从 2 条降到 1 条。少的那条
        #             没有任何人提起过。静默降级这一侧永远更好看。
        #
        # 派发前那道 pre-dispatch-spec-ref 拦不住：它跑在 worker 之前，
        # 那时文档还是原样。注释里「走到这里 resolve 一定成功过」当时是真的，
        # 但它说的是**上一份文本**。
        #
        # 判据只覆盖本任务 spec_ref 解析出的正文，不覆盖整份文档：实测改文档
        # 里别的编号、往尾部追加新条目都不响（合法情形 —— 一个仓库的 PRD 里
        # 有别的任务的章节），只有这几条自己被改/被删才响。口述来源的任务
        # （没有 spec_doc）criteria 恒定，这道闸门对它们永远静默。
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

        # 读取 checks：优先从任务 YAML，如果为空则从 worktree 的 .checks.json 读取
        # （允许 worker 自己定义验证方式）
        checks = task.checks or self._load_worktree_checks(workspace)
        checks = checks + self._runbook_checks(workspace, result)
        regression = self._supervisor.review(workspace, checks)

        # 金丝雀：这一轮动了「能改变裁决」的文件（conftest.py / pyproject.toml
        # 之类）时，验一次那份绿还能不能被推翻。
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
        beacon: SupervisorReport | None = None
        if regression.verdict is Verdict.PASS and (
            touched_judges := judge_files_touched(result.changed_paths)
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
                            f"{res.reason}。这一轮改过："
                            f"{_listed(touched_judges)}",
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

    def _load_worktree_checks(self, workspace: Path) -> tuple:
        """从 worktree 的 .checks.json 读取 worker 自己定义的检查。
        
        允许 worker 在执行过程中决定验证方式（符合"AI 自己决定 checks"的设计）。
        文件不存在或格式错误时返回空元组（静默失败，让监工报 no-checks-defined）。
        """
        import json
        from factory.task import CheckSpec
        
        checks_file = workspace / ".checks.json"
        if not checks_file.exists():
            return ()
        
        try:
            data = json.loads(checks_file.read_text(encoding="utf-8"))
            return tuple(
                CheckSpec(
                    name=c["name"],
                    command=c["command"],
                    expect=c.get("expect", "exit_zero"),
                    value=c.get("value", ""),
                    timeout_s=int(c.get("timeout_s", 300)),
                )
                for c in data.get("checks", [])
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            # 文件格式错误，返回空（监工会报 no-checks-defined）
            return ()

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
