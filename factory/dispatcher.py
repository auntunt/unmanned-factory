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
    changed_hooks,
    diff_suppressed,
    hook_fingerprint,
    neighbour_context,
    runner_hooks,
    shadow_code,
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

            reports = self._review(task, workspace, result, hooks_before)
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
    ) -> tuple[SupervisorReport, ...]:
        """harness 报错 / 空 diff 都算这一轮红，且不跑 check、不调模型监工。

        原因：check 全绿但 agent 什么都没改，说明 check 太弱或任务已完成，
        两种都需要人看一眼，不能静默 merge。没有 diff 时调模型监工纯烧钱。

        `hooks_before` 刻意**没有默认值**：给它填 `frozenset()` 会让漏传的
        调用方在一个本来就装了合法 pre-commit 的仓库上每个任务都判红
        （空基线 vs 有 hook = 差异）。让它当场 TypeError 好过每次都响。
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

        checks = task.checks + self._runbook_checks(workspace, result)
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
        if regression.verdict is Verdict.PASS and (
            touched_judges := judge_files_touched(result.changed_paths)
        ):
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
