"""无人工厂入口。

装好后是 `factory <子命令>`（`[project.scripts]`）；没装时
`python -m factory.cli <子命令>` 等价 —— 定时任务里用前者的绝对路径，
它自带正确的 sys.path，不依赖 cwd。

    prd       录音 / 自由文本 → 任务 YAML 草稿（要人看过再 run）
    run       派发一个任务
    queue     任务入队 / 看队列状态
    loop      跑批循环：认领队列里的任务，直到预算或队列耗尽
    show      打印某个任务的审计轨迹
    override  人工定案 resolution（spec §5：此字段事后填写）
    defect    事后挂 defect，捕获漏报
    metrics   spec §5.1 的三个数，用于两周后裁剪监工
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from factory.audit.models import Resolution
from factory.audit.store import AuditStore
from factory.backlog.journal import Journal, Rollup
from factory.backlog.loop import (
    DEFAULT_BUDGET_USD,
    BacklogLoop,
    Idle,
    LoopLimits,
    TaskRun,
)
from factory.backlog.store import (
    INBOX,
    LOG,
    NEEDS_HUMAN,
    STATES,
    Backlog,
    BacklogError,
)
from factory.dispatcher import Dispatcher, Outcome
from factory.grading.rules import GradingEngine
from factory.harness.base import Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.harness.preflight import PreflightError, resolve_binary
from factory.harness.shell import ShellAdapter
from factory.harness.worktree import WorktreePool
from factory.intake.extract import DraftTask, IntakeError, TaskExtractor
from factory.intake.guard import KNOWN_OPS
from factory.intake.transcribe import TranscribeError, read_source
from factory.metrics import gate3_rework, supervisor_metrics
from factory.runbook import RunbookError, RunbookLibrary
from factory.supervisors.architecture import ArchitectureSupervisor
from factory.supervisors.model_base import ClaudeJudge
from factory.supervisors.spec_review import SpecSupervisor
from factory.task import Task

HARD_GATE_NOTE = (
    "硬闸门：不可逆操作必须由人执行。"
    "agent 只能生成待执行脚本，本管道不代为执行。"
)


def build_adapter(
    harness: str,
    *,
    binary: str,
    shell_argv: list[str] | None = None,
    sandbox: bool = False,
):
    """按名字选 harness。审计表的 harness 字段就是这个名字。

    监工侧的 judge 始终是 claude —— 换 worker 不等于换裁判，
    否则「换个 harness 顺手把审查也换松了」会成为一条绕过验收的路径。

    sandbox 只加在 **worker** 上，不加在监工上：监工只读 diff 文本、不落地
    文件，而且它跑在自己的空 tempdir 里（见 ClaudeJudge 的四个独立性 flag）。
    """
    if harness == "claude_code":
        return ClaudeCodeAdapter(binary=binary, sandbox=sandbox)
    if harness == "shell":
        if not shell_argv:
            raise ValueError("--harness shell 需要 --shell-argv，例如 "
                             "--shell-argv ./codemod.py '{prompt}'")
        return ShellAdapter(shell_argv, name="shell", sandbox=sandbox)
    raise ValueError(f"未知 harness: {harness}")


def build_dispatcher(
    db_path: str | Path,
    *,
    binary: str = "claude",
    timeout_s: int = 900,
    max_turns: int | None = None,
    spec_review: bool = False,
    architecture_review: bool = False,
    judge_model: str = "sonnet",
    harness: str = "claude_code",
    shell_argv: list[str] | None = None,
    judge_binary: str = "claude",
    sandbox: bool = False,
    runbook: RunbookLibrary | None = None,
) -> Dispatcher:
    """两个调模型的监工默认关。它们每轮都花钱，开关交给调用方。"""
    def judge() -> ClaudeJudge:
        return ClaudeJudge(
            binary=judge_binary, model=judge_model, timeout_s=timeout_s
        )

    return Dispatcher(
        runbook=runbook,
        adapter=build_adapter(
            harness, binary=binary, shell_argv=shell_argv, sandbox=sandbox
        ),
        store=AuditStore(db_path),
        spec_supervisor=SpecSupervisor(judge=judge()) if spec_review else None,
        architecture_supervisor=(
            ArchitectureSupervisor(judge=judge()) if architecture_review else None
        ),
        limits=Limits(max_turns=max_turns, timeout_s=timeout_s),
    )


def _print_report(task: Task, report, *, workspace: Path | None = None) -> None:
    print(f"task     : {task.task_id}")
    print(f"outcome  : {report.outcome.value}")
    print(f"rounds   : {report.rounds}")
    print(f"class    : {report.final_grade.oracle_class.value}"
          f"  ({report.final_grade.reason})")
    print(f"attempts : {list(report.attempt_ids)}")
    if workspace is not None:
        print(f"worktree : {workspace}")
    if getattr(report, "commit", None):
        print(f"commit   : {report.commit}  (分支上已提交，主分支未动)")
    elif report.outcome is Outcome.MERGED and getattr(
            report, "landing_note", ""):
        # 判绿了但没落地，要说清楚 —— 否则人会以为产出已经提交了，
        # 而实际上它是 worktree 里一堆未提交的改动，`git worktree remove` 会扔掉。
        print(f"commit   : 未提交 —— {report.landing_note}")
    if report.escalation_reason:
        print(f"reason   : {report.escalation_reason}")
    if report.outcome is Outcome.BLOCKED_HARD_GATE:
        print(HARD_GATE_NOTE)


def _resolve_sandbox(ns: argparse.Namespace) -> int:
    """把三态的 --sandbox/--no-sandbox 化成布尔，并把选择打给人看。

    默认开而不是默认关：无人工厂里"靠人记得加 flag"的防护等于没有防护。
    但 `--sandbox` 显式要求时平台不支持要**报错**，不能静默降级 —— 那正是
    "以为隔离生效而实际没有"的情形。没显式要求时静默关掉，好让流水线在
    Linux 上仍能跑（那边该用容器，不是这一层）。
    """
    from factory.harness import sandbox as sb

    if ns.sandbox is False:
        print("⚠ 沙箱已关闭：worker 能写 $HOME、系统目录、以及本工厂自己的代码"
              "（含分级规则）。", file=sys.stderr)
        return 0
    if sb.available():
        ns.sandbox = True
        return 0
    if ns.sandbox is True:
        print(f"--sandbox 要求隔离，但本机没有 {sb.SANDBOX_BINARY}（非 macOS?）。"
              "不静默降级：要么去掉 --sandbox，要么换容器。", file=sys.stderr)
        return 2
    ns.sandbox = False
    print("提示：本平台无 Seatbelt，worker 未隔离副作用。", file=sys.stderr)
    return 0


def _cmd_run(ns: argparse.Namespace) -> int:
    if (rc := _resolve_sandbox(ns)) != 0:
        return rc
    tasks = [Task.from_yaml(p) for p in ns.task]
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        # 同一 task_id 并行跑会撞 attempt_no，也会抢同一个 worktree 目录。
        print(f"task_id 重复：{dupes}。同一任务不能在一次派发里出现两次。",
              file=sys.stderr)
        return 2

    # harness 配错要当场报错退出。绝不能默默退回 claude_code —— 那等于
    # 在用户以为换了 worker 的情况下偷偷用了另一个，审计表也会记错。
    try:
        build_adapter(ns.harness, binary=ns.binary, shell_argv=ns.shell_argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 规则库配错也要在派发**之前**炸。等到监工阶段才发现 YAML 坏了，
    # worker 的 token 已经烧完了 —— 而且那一轮会以一个跟规则库毫无字面
    # 关联的错误结束。
    try:
        _runbook_for(ns)
    except RunbookError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if len(tasks) == 1 and not ns.worktree:
        return _run_one(ns, tasks[0], Path(ns.workspace))
    return _run_pool(ns, tasks)


def _dispatcher_for(ns: argparse.Namespace) -> Dispatcher:
    return build_dispatcher(
        ns.db,
        binary=ns.binary,
        timeout_s=ns.timeout,
        max_turns=ns.max_turns,
        spec_review=ns.spec_review,
        architecture_review=ns.architecture_review,
        judge_model=ns.judge_model,
        harness=ns.harness,
        shell_argv=ns.shell_argv,
        # judge 固定用 claude：--binary 换的是 worker，不是裁判。
        judge_binary=ns.judge_binary,
        sandbox=ns.sandbox,
        runbook=_runbook_for(ns),
    )


def _runbook_for(ns: argparse.Namespace) -> RunbookLibrary | None:
    """按 flag 装配规则库。都没给就返回 None（只跑任务自带的 check）。

    不默认开全局规则：那会让每个既有任务的检查集在升级工厂后悄悄变大，
    突然多出来的打回没人对得上原因。要继承得显式说。
    """
    project = getattr(ns, "runbook", None)
    if not getattr(ns, "global_runbook", False) and project is None:
        return None
    return RunbookLibrary.load(
        project=project,
        workspace=Path(ns.workspace),
        include_global=getattr(ns, "global_runbook", False),
    )


def _run_one(ns: argparse.Namespace, task: Task, workspace: Path) -> int:
    report = _dispatcher_for(ns).run(task, workspace)
    _print_report(task, report)
    return 0 if report.outcome is Outcome.MERGED else 1


def _run_pool(ns: argparse.Namespace, tasks: list[Task]) -> int:
    """每个任务一棵 worktree，最多 ns.parallel 个同时跑。

    每个线程建自己的 Dispatcher（也就是自己的 AuditStore/adapter/judge）——
    共享一个 Session 不是线程安全的，而 SQLite 侧的并发已经在 store 里处理了。
    """
    pool = WorktreePool(Path(ns.workspace), root=ns.worktree_root)
    n = max(1, min(ns.parallel, len(tasks)))
    print(f"派发 {len(tasks)} 个任务，并发 {n}，worktree 根目录 {pool.root}\n")

    def one(task: Task) -> tuple[Task, object, Path | None, str]:
        try:
            wt = pool.acquire(task.task_id)
        except Exception as exc:  # worktree 开不出来 → 这个任务算失败，别拖累别的
            return task, None, None, f"worktree 创建失败：{exc}"
        try:
            report = _dispatcher_for(ns).run(task, wt.path)
        except Exception as exc:
            return task, None, wt.path, f"派发异常：{type(exc).__name__}: {exc}"
        return task, report, wt.path, ""

    results: list[tuple[Task, object, Path | None, str]] = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = {ex.submit(one, t): t for t in tasks}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: tasks.index(r[0]))
    merged = 0
    for task, report, path, err in results:
        if err:
            print(f"task     : {task.task_id}")
            print(f"outcome  : error")
            if path is not None:
                print(f"worktree : {path}")
            print(f"reason   : {err}\n")
            continue
        _print_report(task, report, workspace=path)
        print()
        if report.outcome is Outcome.MERGED:
            merged += 1

    print(f"== {merged}/{len(tasks)} merged")
    print("worktree 保留未删：产出还没人验收过。看完后手动 "
          "`git worktree remove <path>`。")
    return 0 if merged == len(tasks) else 1


def _cmd_prd(ns: argparse.Namespace) -> int:
    """自由文本 / 录音 → Task YAML 草稿。

    产物不直接进 dispatcher —— 要人看过才 factory run。
    见 DraftTask.to_yaml() 里的注释块，原因在那里写得更清楚。
    """
    try:
        description = read_source(
            text=ns.text,
            text_file=ns.text_file,
            audio=ns.audio,
            binary=ns.whisper_binary,
            model=ns.whisper_model,
            language=ns.language,
        )
    except TranscribeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    extractor = TaskExtractor(binary=ns.binary, model=ns.intake_model)
    try:
        draft: DraftTask = extractor.run(description)
    except IntakeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if ns.dry_run:
        print(draft.to_yaml())
        if draft.unclear:
            print(f"# 模型的疑问（共 {len(draft.unclear)} 条）—— "
                  "改写描述后重跑：", file=sys.stderr)
            for u in draft.unclear:
                print(f"#   - {u}", file=sys.stderr)
        return 0

    # 补 check 必须在闸门之前：闸门的第一条硬拦截就是「没有可执行的 check」。
    # 放在闸门之后等于永远补不上 —— 该补的那些草稿已经落进 needs-human 了。
    if ns.propose_checks and not draft.checks:
        draft = _propose_checks(draft, ns)

    if ns.queue:
        return _admit_to_queue(draft, ns)

    out_path = Path(ns.output or f"tasks/{draft.task_id}.yaml")
    written = draft.write(out_path)
    print(f"已写入 {written}")
    print(f"  task_id   : {draft.task_id}")
    print(f"  ops       : {list(draft.declared_ops) or '[]'}")
    if draft.guard_findings:
        print(f"  guard补充 : {[f.op for f in draft.guard_findings]}")
    if draft.unclear:
        print(f"  待确认    : {list(draft.unclear)}")
    cost = f"${draft.cost_usd:.4f}" if draft.cost_usd else "-"
    print(f"  tokens    : {draft.tokens}  cost={cost}")
    print(f"\n下一步：确认 {written}，然后：")
    print(f"  factory run {written} --workspace <仓库路径> --db audit.db")
    return 0


def _propose_checks(draft: DraftTask, ns: argparse.Namespace) -> DraftTask:
    """acceptance → 探过针的 checks，补进草稿。

    只在 draft.checks 为空时调用（见调用点）。用户自己写了验收命令就不覆盖：
    他写的那条是需求的一部分，模型的提议只是补空缺。

    失败一律**原样返回**，不抛：草稿带着空 checks 往下走，闸门那条
    「没有可执行的 check」会把它拦进 needs-human。这是 fail-closed ——
    提议失败的后果退回到「要人补」，也就是没有这个功能之前的状态。
    """
    from factory.intake.checkgen import CheckGenError, CheckProposer

    if not ns.workspace:
        print("  提议check  : 跳过 —— 没给 --workspace，探针无处可跑",
              file=sys.stderr)
        return draft

    proposer = CheckProposer(binary=ns.binary, model=ns.intake_model)
    try:
        prop = proposer.propose(
            acceptance=draft.acceptance,
            prompt=draft.prompt,
            workspace=Path(ns.workspace),
        )
    except CheckGenError as exc:
        print(f"  提议check  : 失败 —— {exc}", file=sys.stderr)
        return draft

    for line in prop.lines():
        print(f"  {line}")
    if not prop.checks:
        return draft

    # tokens / cost 累加进草稿：提议这一步的钱是这张草稿花的，
    # 不累加的话 metrics 里入口层的成本会少算一半。
    return replace(
        draft,
        checks=prop.checks,
        tokens=draft.tokens + prop.tokens,
        cost_usd=draft.cost_usd + prop.cost_usd,
    )


def _admit_to_queue(draft: DraftTask, ns: argparse.Namespace) -> int:
    """草稿过闸门 → 进 inbox；不过 → 落 needs-human 等人。

    这是「无人」最后一段：在这之前任务从哪来始终得有人动手。闸门不取消
    DraftTask 那句「要人看一眼」，而是把它变成可判定的条件（见 intake/gate.py）。

    不过闸门的草稿**照样落盘**，落在 needs-human/。丢掉的话，一次口述需求
    就白说了，而人连"差什么"都看不到 —— 那比要求人确认更糟。

    退出码：进队 0，被拦 3。3 而不是 1：被拦是闸门在正常工作，
    不是错误；但也不能是 0，否则 `factory prd --queue && factory loop`
    会在什么都没入队的情况下继续跑。
    """
    from factory.intake.gate import admit

    verdict = admit(draft)
    bl = Backlog(ns.queue).ensure()
    state = INBOX if verdict.admitted else NEEDS_HUMAN
    dst = bl.dir(state) / f"{draft.task_id}.yaml"
    if dst.exists():
        # 同名不覆盖，和 _park 一个道理：覆盖会让先到的那个静默消失。
        n = 2
        while dst.exists():
            dst = dst.with_name(f"{draft.task_id}.{n}.yaml")
            n += 1
    draft.write(dst)

    # 判决写进队列日志，和 loop 的 dispatch 事件同一份文件。
    # 只打 stdout 是不够的：闸门的误拒率要用「拦了什么」对上「人后来怎么处置」，
    # 而 stdout 在下一次 prd 之后就没了。Journal.event 写不进去也不抛。
    Journal(bl.dir(LOG)).event(
        "gate",
        task_id=draft.task_id,
        admitted=verdict.admitted,
        codes=list(verdict.codes),
        path=str(dst),
        cost_usd=draft.cost_usd,
    )

    print(f"已写入 {dst}")
    print(f"  task_id   : {draft.task_id}")
    for w in verdict.warnings:
        print(f"  提示      : {w}")

    if verdict.admitted:
        print("\n闸门通过 → 已进 inbox，等 `factory loop` 认领。")
        print("  注意：闸门只判「声明可不可信」，不做分级。"
              "C 类永不无人、D 类硬闸门仍在派发时判。")
        return 0

    print(f"\n闸门拦下（{len(verdict.reasons)} 条）→ 落在 needs-human，等人：")
    for r in verdict.reasons:
        print(f"  - {r}")
    print(f"\n补齐后入队：factory queue {dst} --queue {ns.queue}")
    return 3


def _cmd_loop(ns: argparse.Namespace) -> int:
    """跑批循环。这是「无人」真正落地的地方 —— 前面所有子命令都要人敲一次。

    配置错误全部在**进入循环之前**验证。循环一旦跑起来就没人看着了，
    此时报「harness 名字打错了」意味着队列被整个刷成 error。
    """
    if (rc := _resolve_sandbox(ns)) != 0:
        return rc
    try:
        build_adapter(ns.harness, binary=ns.binary, shell_argv=ns.shell_argv)
        # binary 能不能跑，只在 loop 里查。build_adapter 只校验名字，
        # 而 launchd 的 PATH 里通常没有 nvm 装的 claude —— 不在这儿拦，
        # 整条队列会被逐个刷成 error，且 version() 吞掉 OSError 后毫无信号。
        argv0 = ns.shell_argv[0] if ns.harness == "shell" and ns.shell_argv \
            else ns.binary
        print(f"worker   : {resolve_binary(argv0)}")
        _runbook_for(ns)
    except (ValueError, RunbookError, PreflightError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    bl = Backlog(ns.queue).ensure()
    limits = LoopLimits(
        max_tasks=ns.max_tasks,
        budget_usd=ns.budget_usd,
        max_runtime_s=ns.max_runtime,
        poll_s=ns.poll,
        idle=Idle(ns.idle),
        max_unpriced_streak=ns.max_unpriced_streak,
    )
    if not limits.budget_usd:
        print("⚠ --budget-usd 0：没有花费上限。这个循环会一直派发到队列空 "
              "(--idle watch 下永不空)，没人看着的时候也一样。", file=sys.stderr)
    if not limits.max_runtime_s:
        # 光有预算上限不够：超时的 attempt 记 $0（被 kill 的进程不打 usage
        # payload），所以反复超时的任务在预算眼里是免费的。墙钟是超时躲不过
        # 的那道闸。单个任务最坏 max_rounds x --timeout，默认 3 x 900s。
        print("⚠ --max-runtime 0：没有墙钟上限。超时的 attempt 记 $0，"
              "光靠 --budget-usd 拦不住反复超时的任务。", file=sys.stderr)
    if not limits.max_unpriced_streak:
        print("⚠ --max-unpriced-streak 0：熔断关了。CLI 一直挂着的时候，"
              "每个任务都记 $0，预算闸门读到的永远是 0。", file=sys.stderr)

    pool = WorktreePool(Path(ns.workspace), root=ns.worktree_root) \
        if ns.worktree else None
    print(f"queue={bl.root}  workspace={ns.workspace}  idle={limits.idle}")
    print(f"上限：任务 {limits.max_tasks or '∞'} / "
          f"预算 ${limits.budget_usd or 0:.2f}" +
          ("（无限）" if not limits.budget_usd else "") +
          f" / 时长 {limits.max_runtime_s or '∞'}s"
          f" / 连续漏账 {limits.max_unpriced_streak or '∞'}")

    loop = BacklogLoop(bl, lambda p: _dispatch_queued(ns, p, pool),
                       limits=limits)
    loop.install_signal_handlers()
    report = loop.run()
    if report.recovered:
        print(f"另有 {report.recovered} 个崩溃残留被移到 needs-human")
    # 退出码只看有没有异常。escalated 不算失败 —— 它是设计里的正常出口
    # （3 轮没过就交给人），把它当失败会让 cron 每天都报警。
    return 1 if report.errors else 0


def _dispatch_queued(ns: argparse.Namespace, task_path: Path,
                     pool: WorktreePool | None) -> TaskRun:
    """派发队列里的一个条目，把 DispatchReport 翻成 TaskRun。

    Task.from_yaml 的异常刻意不在这里接 —— 循环的 _one() 会把它归档成
    error 并写进 .result.json，那里的处理比这里多（原文入档）。
    """
    task = Task.from_yaml(task_path)
    workspace = _queued_workspace(ns, task, pool)
    report = _dispatcher_for(ns).run(task, workspace)
    cost, unpriced = _attempts_cost(ns, report.attempt_ids)
    note = report.escalation_reason.replace("\n", " ")[:400]
    # 只有真的开了 worktree 才写进 note。C/D 类任务拿到的是 --workspace 本身，
    # 标成 worktree= 会让人以为那里有产出可看，而实际上一行都没跑。
    if workspace != Path(ns.workspace):
        note = f"worktree={workspace}  {note}".strip()
    # sha 进 note 是给夜跑用的：第二天早上从 journal 就能直接 git show，
    # 不必先查审计库。审计库仍是权威，这里只是近路。
    if report.commit:
        note = f"commit={report.commit[:12]}  {note}".strip()
    elif report.outcome is Outcome.MERGED and report.landing_note:
        note = f"未提交（{report.landing_note}）  {note}".strip()
    return TaskRun(outcome=str(report.outcome), cost_usd=cost, note=note,
                   unpriced=unpriced)


def _queued_workspace(ns: argparse.Namespace, task: Task,
                      pool: WorktreePool | None) -> Path:
    """给这个任务准备工作目录。**预分级 C/D 的任务不开 worktree。**

    实测发现的：一个 declared_paths 里有 deploy.sh 的任务会被硬闸门在派发前
    拦掉，但 worktree 已经建好了 —— 于是 .factory-worktrees/ 里堆着一堆
    空目录，每个都代表「一个从未跑过的任务」。而 worktree 目录的存在本身
    在别处是有含义的（「这里有产出没人验收」），堆着空的会把那个信号淹掉。

    这里重跑一次预分级只是为了决定要不要开目录，**不是**闸门 —— 真正的
    判定在 Dispatcher.run 里，那条路径一步都没绕过。这里判错的最坏后果是
    多开或少开一个空目录。
    """
    if pool is None:
        return Path(ns.workspace)
    grade = GradingEngine.default().grade(task.declared_paths,
                                          task.declared_ops)
    if not grade.unmanned_allowed:
        return Path(ns.workspace)
    return pool.acquire(task.task_id).path


def _attempts_cost(ns: argparse.Namespace, attempt_ids: tuple[int, ...]
                   ) -> tuple[float, bool]:
    """一次派发的真实花费，以及「这个数低估了没有」。

    从审计库读而不是让 dispatcher 返回：预算闸门必须和账单看同一个数。
    dispatcher 另算一份的话，两个数会在某次重构后悄悄分叉，而分叉的方向
    只有在账单上才看得出来。

    监工的花费也要算：它们记在 verdict 行上，不在 attempt 行上。只算
    attempt 的话，开了 --spec-review 的循环会系统性低估自己的开销 ——
    低估的预算闸门等于没有闸门。

    **超时的 attempt 记 $0，而它真的花了钱。** 花费只来自 CLI 的 JSON
    payload，而被我们 kill 掉的进程永远不会打出那个 payload。真跑实测：
    一次 900s 超时的 attempt 记 `cost=$0.0000`，但它的 transcript 里有
    ~232k input tokens。所以这里对超时 attempt 打一行警告 —— 不猜价格
    （transcript 里没有 cost 字段，估算需要一张价目表，而估错的账单比
    没有账单更难查），但也不让它静默读成 0。
    """
    store = AuditStore(ns.db)
    total = 0.0
    leaked = False
    for aid in attempt_ids:
        try:
            row = store.get(aid)
        except Exception:   # noqa: BLE001 - 花费读不到不该让整个循环停
            continue
        if row is None:
            continue        # get() 查不到时返回 None 而不是抛，上面兜不住
        total += row.cost_usd or 0.0
        total += sum(v.cost_usd or 0.0 for v in row.supervisors)
        leaked |= _warn_if_untracked_spend(row)
    return total, leaked


def is_untracked_spend(row) -> bool:
    """这个 attempt 超时了、记 $0，但真的花了钱。

    判据是 regression 监工的 claim 文本里有 'timeout'：超时在审计库里就是
    这么落的（`期望 'exit_status ok' / 实得 'timeout: timeout after 900s'`），
    没有单独的状态列。文本判据不好看，但比在审计模型上加一列小得多。

    返回布尔而不是只打警告：循环要靠这个数做熔断（连续 N 个任务都在烧
    不计价的钱就停机）。只打警告的话，那道闸没有数据可依。
    """
    if row.cost_usd:
        return False
    for v in row.supervisors:
        for c in (v.claims or ()):
            # claims 是 JSON 列，取出来就是 dict。
            got = str((c or {}).get("got", "")) if isinstance(c, dict) else ""
            if "timeout" in got.lower():
                return True
    return False


def _warn_if_untracked_spend(row) -> bool:
    """漏账就说出来，别让预算闸门静默读成 0。返回是否漏账。"""
    if not is_untracked_spend(row):
        return False
    print(f"⚠ attempt #{row.attempt_no} 超时且记 $0 —— "
          f"真实花费未计入预算（transcript: "
          f"{row.transcript_path or '未留'}）", file=sys.stderr)
    return True


def _cmd_queue(ns: argparse.Namespace) -> int:
    """入队 / 看状态。刻意没有 `queue clear` —— 见 _cmd_queue_status 的注释。"""
    bl = Backlog(ns.queue).ensure()
    if getattr(ns, "history", 0):
        return _cmd_queue_history(bl, ns.history)
    if not ns.task:
        return _cmd_queue_status(bl)
    added = []
    for p in ns.task:
        try:
            added.append(bl.add(p))
        except BacklogError as exc:
            print(str(exc), file=sys.stderr)
            return 2      # 一个都不入队：部分成功比全失败更难收拾
    # 从 needs-human/ 入队 = 人推翻了闸门的判决。这是误拒的**唯一**信号：
    # 闸门自己永远不知道它拦错了，只有人把那份草稿原样放行才说明它该过。
    # 记在这里而不是等人填表 —— 需要额外动作的度量等于没有度量。
    journal = Journal(bl.dir(LOG))
    parked = bl.dir(NEEDS_HUMAN).resolve()
    overruled = set()
    for src, dst in zip(ns.task, added):
        if Path(src).expanduser().resolve().parent == parked:
            journal.event("gate_overruled", task_id=dst.stem, path=str(dst),
                          note="人把 needs-human 的草稿放回 inbox")
            overruled.add(dst)

    for path in added:
        print(f"已入队 {path}")
        if path in overruled:
            print("  ↑ 人推翻了闸门判决，已记进日志"
                  "（factory queue --history 里看误拒率）")
    print(f"\ninbox 现有 {len(bl.pending())} 个。开始跑：")
    print(f"  factory loop --queue {bl.root} --workspace <仓库路径> --db audit.db")
    return 0


def _cmd_queue_history(bl: Backlog, limit: int) -> int:
    """回答「昨晚跑得怎么样」。

    退出码刻意**总是 0**，包括有待人介入的任务时。这是个查询命令，不是闸门；
    非零退出会让它没法放进 `&&` 链，也会让 cron 的日报邮件变成告警邮件。
    """
    journal = Journal(bl.dir(LOG))
    events = journal.tail(limit=limit)
    if not events:
        print(f"{bl.dir(LOG)} 里还没有日志（跑过 factory loop 才会有）")
        return 0
    roll = Rollup(events)
    print(f"最近 {len(events)} 条事件（{bl.dir(LOG)}）")
    for line in roll.lines():
        print(f"  {line}")
    return 0


def _cmd_queue_status(bl: Backlog) -> int:
    """打印各状态计数。

    没有 `queue clear` 子命令是刻意的：done/ 和 needs-human/ 里的
    .result.json 是「这个任务花了多久、为什么被打回」的唯一现场记录，
    而 audit.db 里没有队列侧的时间。一个 clear 命令会让「清一下队列」
    这种顺手操作把它们一起删掉。要删就自己 rm，那时你知道自己在删什么。
    """
    c = bl.counts()
    print(f"queue: {bl.root}")
    for state in STATES:
        print(f"  {state:<12}{c[state]}")
    if c["running"]:
        print("\nrunning/ 里的条目：")
        for p in bl.running():
            claim = bl.read_claim(p)
            print(f"  {p.name}  pid={claim.get('pid', '?')} "
                  f"host={claim.get('host', '?')}")
    return 0


def _cmd_show(ns: argparse.Namespace) -> int:
    attempts = AuditStore(ns.db).attempts_for(ns.task_id)
    if not attempts:
        print(f"没有 task_id={ns.task_id} 的记录")
        return 1

    print(f"== {ns.task_id} ==  {len(attempts)} 次尝试")
    for row in attempts:
        print(f"\n-- attempt #{row.attempt_no}  (id={row.id})")
        print(f"   class      : {row.oracle_class} ({row.class_reason})")
        print(f"   harness    : {row.harness} {row.harness_version}"
              f"  model={row.model}")
        print(f"   spec_ref   : {list(row.spec_ref)}")
        print(f"   diff_hash  : {row.diff_hash or '-'}")
        print(f"   commit     : {row.commit or '-'}")
        print(f"   transcript : {row.transcript_path or '-'}")
        print(f"   tokens     : in={row.tokens_in} out={row.tokens_out}"
              f"  cost=${row.cost_usd:.4f}  {row.wall_clock_ms}ms")
        print(f"   created_at : {row.created_at}")
        print(f"   resolution : {row.resolution}")
        print(f"   defects    : {list(row.linked_defects)}")
        for v in row.supervisors:
            print(f"   [{v.role}] {v.verdict}"
                  f"  tokens={v.tokens} cost=${v.cost_usd:.4f}")
            for c in v.claims:
                print(f"       - {c.get('check')}: 期望 {c.get('expected')!r}"
                      f" / 实得 {c.get('got')!r}")
    return 0


def _cmd_override(ns: argparse.Namespace) -> int:
    store = AuditStore(ns.db)
    if not store.exists(ns.attempt_id):
        print(f"没有 attempt id={ns.attempt_id}")
        return 1
    store.finalize(ns.attempt_id, Resolution(ns.resolution))
    print(f"attempt {ns.attempt_id} → resolution={ns.resolution}")
    return 0


def _cmd_defect(ns: argparse.Namespace) -> int:
    """挂 defect。attempt_id 或 --commit 二选一。

    --commit 走的是 spec §5 写明的漏报回查路径：发现 bug → git blame →
    commit → task_id → 当时哪个监工放过了。
    """
    store = AuditStore(ns.db)
    attempt_id = ns.attempt_id

    if ns.commit:
        if attempt_id is not None:
            print("attempt_id 和 --commit 只能给一个", file=sys.stderr)
            return 2
        row = store.attempt_by_commit(ns.commit)
        if row is None:
            # 两种情形合成一句：查不到，和短 sha 撞了多条。区分它们要多打一次
            # 库，而人接下来的动作是一样的 —— 给全 sha 再试。
            print(f"commit {ns.commit} 查不到唯一的 attempt"
                  "（没这条记录，或者短 sha 命中多条 —— 给全 40 位再试）",
                  file=sys.stderr)
            return 1
        attempt_id = row.id
        print(f"commit {ns.commit} → task_id={row.task_id} "
              f"attempt #{row.attempt_no} (id={attempt_id})")
    elif attempt_id is None:
        print("要么给 attempt_id，要么给 --commit", file=sys.stderr)
        return 2
    elif not store.exists(attempt_id):
        print(f"没有 attempt id={attempt_id}")
        return 1

    store.link_defect(attempt_id, ns.defect_id)
    row = store.get(attempt_id)
    print(f"attempt {attempt_id} defects={list(row.linked_defects)}")
    # 漏报的意义在于「本该哪个监工拦住」，所以把当时的裁决一并打出来 ——
    # 不打的话人还得再敲一次 show 才知道该改哪个监工。
    for v in row.supervisors:
        print(f"  当时 [{v.role}] 判了 {v.verdict}")
    return 0


def _cmd_metrics(ns: argparse.Namespace) -> int:
    store = AuditStore(ns.db)
    report = supervisor_metrics(store, task_id=ns.task_id)
    if not report:
        print("没有裁决数据")

    for role, m in sorted(report.items()):
        rate = "—" if m.hit_rate is None else f"{m.hit_rate:.0%}"
        per_hit = "—" if m.cost_per_hit is None else f"${m.cost_per_hit:.4f}"
        print(f"[{role}]")
        print(f"  命中率      : {rate}"
              f"  (真阳性 {m.true_positives} / 已定案 {m.adjudicated})")
        print(f"  漏报        : {m.false_negatives}")
        print(f"  单位命中成本: {per_hit}"
              f"  (总 ${m.cost_usd:.4f} / {m.tokens} tokens)")
        print(f"  报警 {m.fired} 次，放行 {m.passed} 次，"
              f"未定案 {m.unadjudicated} 次，自身故障 {m.faults} 次")
        print(f"  → {m.verdict_line()}")

    # 闸门 3 是 spec §9 的 P1 判据。它不随 --task-id 收窄：单个任务的
    # 打回次数说明不了验收系统好不好，只有跨任务的均值有意义。
    g = gate3_rework(store)
    mean = "—" if g.mean_reworks is None else f"{g.mean_reworks:.2f}"
    print("[闸门 3]")
    print(f"  上人平均打回次数: {mean}  (目标 ≤ {g.target:g}，"
          f"{g.total_reworks} 次打回 / {g.tasks} 个已派发任务)")
    if g.meets_p1_target is None:
        print("  → 尚无已派发任务，P1 判据待测")
    elif g.meets_p1_target:
        print("  → 达标：验收系统的判据基本对得上人的判断")
    else:
        print("  → 未达标：打回多是判据没写对，不是 agent 不行 —— 改 checks")
    return 0


def _add_dispatch_args(p: argparse.ArgumentParser) -> None:
    """run 和 loop 共用的派发参数。

    共用一份而不是各写一遍：这里面有沙箱、规则库、judge binary 这些安全相关
    的默认值。两份定义会漂移，而漂移的方向不可预测 —— 「loop 的沙箱默认没
    跟上 run」这种 bug 不会有任何报错，只会在某天变成一个写了 $HOME 的 worker。
    """
    p.add_argument("--workspace", required=True, help="目标 git 仓库")
    p.add_argument("--worktree-root", default=None,
                   help="worktree 存放目录，默认仓库同级的 .factory-worktrees")
    p.add_argument("--db", default="audit.db")
    p.add_argument("--binary", default="claude")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--spec-review", action="store_true",
                   help="开规格监工（调模型，按轮计费）")
    p.add_argument("--architecture-review", action="store_true",
                   help="开架构监工（调模型；其意见不能单独否决合并）")
    p.add_argument("--judge-model", default="sonnet",
                   help="两个调模型监工用的档位")
    p.add_argument("--harness", default="claude_code",
                   choices=("claude_code", "shell"),
                   help="worker 用哪个 harness（默认 claude_code）")
    # action="extend"：重复给 --shell-argv 要累加而不是覆盖。
    # 默认的 nargs="+" 会让后一个 flag 顶掉前一个，于是 argv[0] 变成 "{prompt}"，
    # 报出来是 "cannot launch {prompt}" —— 排查起来完全看不出是参数被吞了。
    p.add_argument("--shell-argv", nargs="+", action="extend", default=None,
                   help="--harness shell 的命令行，支持 {prompt} / {workspace} 占位符")
    # 默认开（能开的话）。理由：这是个**无人**工厂 —— 靠人记得加 flag 的防护
    # 等于没有防护。所以反过来，不要隔离得显式说 --no-sandbox，会打印一行提醒。
    # 平台不支持时自动关，不报错：否则整条流水线在 Linux 上直接跑不起来。
    p.add_argument("--sandbox", action="store_true", default=None,
                   help="强制开启沙箱；平台不支持时报错退出（默认已在 macOS 上自动开）")
    p.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                   help="关掉沙箱。worker 将能写 $HOME、系统目录、以及本工厂"
                        "自己的代码（包括分级规则）")
    p.add_argument("--judge-binary", default="claude",
                   help="监工用的 CLI。换 worker 不换裁判，所以和 --binary 分开")
    # runbook 规则库（spec §8）。默认全关：升级工厂不该让既有任务的检查集
    # 悄悄变大，那样多出来的打回没人对得上原因。
    p.add_argument("--global-runbook", action="store_true",
                   help="启用内置全局 runbook 规则（docker restart 陷阱等）")
    p.add_argument("--runbook", default=None,
                   help="项目规则 YAML 路径。同名规则覆盖全局。"
                        "不能放在 workspace 里 —— worker 能写 workspace，"
                        "从那儿读规则等于让它自己出卷子")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    prd = sub.add_parser("prd", help="录音 / 自由文本 → 任务 YAML 草稿")
    prd.add_argument("--text", default=None, help="直接给需求文字")
    prd.add_argument("--text-file", default=None, help="从文件读需求")
    prd.add_argument("--audio", default=None, help="录音文件（走 whisper 转录）")
    prd.add_argument("-o", "--output", default=None,
                     help="输出 YAML 路径，默认 tasks/<task_id>.yaml")
    prd.add_argument("--dry-run", action="store_true",
                     help="只打到 stdout，不落盘")
    prd.add_argument("--queue", default=None, metavar="DIR",
                     help="过闸门后直接进队列（不过则落 needs-human 等人）。"
                          "退出码 3 = 被闸门拦下")
    prd.add_argument("--propose-checks", action="store_true",
                     help="checks 为空时自动提议（需要 --workspace）。"
                          "提议失败不抛，退化回「等人补 check」")
    prd.add_argument("--workspace", default=None, metavar="DIR",
                     help="仓库路径，供 --propose-checks 跑探针")
    prd.add_argument("--binary", default="claude", help="做提取的 CLI")
    prd.add_argument("--intake-model", default="sonnet",
                     help="提取用的模型档位（结构化转写，不需要最强档）")
    prd.add_argument("--whisper-binary", default="whisper")
    prd.add_argument("--whisper-model", default="small")
    prd.add_argument("--language", default=None,
                     help="转录语言，例如 zh。省略则自动检测")
    prd.add_argument("--list-ops", action="version",
                     version="guard 识别的 ops: " + " ".join(KNOWN_OPS),
                     help="打印 guard 能扫出的 declared_ops")
    prd.set_defaults(func=_cmd_prd)

    run = sub.add_parser("run", help="派发一个或多个任务")
    run.add_argument("task", nargs="+", help="任务 YAML 路径（可多个）")
    run.add_argument("--worktree", action="store_true",
                     help="即使只有一个任务也开独立 worktree（多任务时自动开）")
    run.add_argument("--parallel", type=int, default=1,
                     help="同时跑几个任务（默认 1，串行）")
    _add_dispatch_args(run)
    run.set_defaults(func=_cmd_run)

    q = sub.add_parser("queue", help="任务入队 / 看队列状态")
    q.add_argument("task", nargs="*", help="任务 YAML 路径。省略则打印队列状态")
    q.add_argument("--queue", default="backlog", help="队列根目录")
    q.add_argument("--history", nargs="?", type=int, const=50, default=0,
                   metavar="N",
                   help="不看当前状态，看跑批日志汇总：花了多少、"
                        "有几个待人介入、循环有没有非正常退出（默认最近 50 条）")
    q.set_defaults(func=_cmd_queue)

    lp = sub.add_parser("loop", help="跑批：不断认领队列里的任务并派发")
    lp.add_argument("--queue", default="backlog", help="队列根目录")
    # 循环里默认开 worktree：一个接一个往同一棵工作树上写，前一个没验收的
    # 改动会变成后一个的既有状态，两个任务的 diff 就分不开了。
    lp.add_argument("--worktree", action="store_true", default=True,
                    help="每个任务一棵 worktree（默认开）")
    lp.add_argument("--no-worktree", dest="worktree", action="store_false",
                    help="全部任务共用 --workspace。前一个任务未验收的改动会"
                         "成为后一个的起点，只在确定性 codemod 上才安全")
    lp.add_argument("--idle", default=Idle.WATCH.value,
                    choices=[i.value for i in Idle],
                    help="队列空了怎么办：watch 继续等（默认）/ drain 抽干就退"
                         "（cron 用）/ once 只跑一个")
    lp.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD,
                    help=f"总花费上限，超了就停（默认 ${DEFAULT_BUDGET_USD:g}；"
                         f"0 = 不限，会打印警告）")
    lp.add_argument("--max-tasks", type=int, default=0,
                    help="最多派发几个任务（0 = 不限）")
    lp.add_argument("--max-runtime", type=float, default=0.0,
                    help="最长运行秒数（0 = 不限）")
    lp.add_argument("--max-unpriced-streak", type=int,
                    default=LoopLimits.max_unpriced_streak,
                    help=f"连续几个任务的花费记不上账（超时被 kill，CLI 没打"
                         f"出 cost）就停机。默认 "
                         f"{LoopLimits.max_unpriced_streak}；0 = 不限，会打印"
                         f"警告 —— 关掉它等于让预算闸门在超时这条路上失效")
    lp.add_argument("--poll", type=float, default=5.0,
                    help="--idle watch 下队列空时的轮询间隔秒数")
    _add_dispatch_args(lp)
    lp.set_defaults(func=_cmd_loop, parallel=1)

    show = sub.add_parser("show", help="打印一个任务的审计轨迹")
    show.add_argument("task_id")
    show.add_argument("--db", default="audit.db")
    show.set_defaults(func=_cmd_show)

    ov = sub.add_parser("override", help="人工定案 resolution（事后回填）")
    ov.add_argument("attempt_id", type=int)
    ov.add_argument("resolution", choices=[r.value for r in Resolution])
    ov.add_argument("--db", default="audit.db")
    ov.set_defaults(func=_cmd_override)

    df = sub.add_parser("defect", help="事后挂 defect，捕获漏报")
    # attempt_id 变成可选：spec §5 的回查路径起点是 git blame，那里只有 sha。
    # 逼人先 show 一遍才能拿到 attempt_id，等于给漏报统计加一道摩擦。
    df.add_argument("attempt_id", type=int, nargs="?", default=None)
    df.add_argument("defect_id")
    df.add_argument("--commit", default=None, metavar="SHA",
                    help="按 commit 反查 attempt（git blame 给的短 sha 就行）"
                         "，替代 attempt_id")
    df.add_argument("--db", default="audit.db")
    df.set_defaults(func=_cmd_defect)

    mx = sub.add_parser("metrics", help="监工命中率 / 漏报 / 单位命中成本")
    mx.add_argument("--task-id", default=None, help="省略则统计全库")
    mx.add_argument("--db", default="audit.db")
    mx.set_defaults(func=_cmd_metrics)

    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
