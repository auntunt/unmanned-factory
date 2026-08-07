"""无人工厂入口。

    prd       录音 / 自由文本 → 任务 YAML 草稿（要人看过再 run）
    run       派发一个任务
    show      打印某个任务的审计轨迹
    override  人工定案 resolution（spec §5：此字段事后填写）
    defect    事后挂 defect，捕获漏报
    metrics   spec §5.1 的三个数，用于两周后裁剪监工
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from factory.audit.models import Resolution
from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.harness.base import Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.harness.shell import ShellAdapter
from factory.harness.worktree import WorktreePool
from factory.intake.extract import DraftTask, IntakeError, TaskExtractor
from factory.intake.guard import KNOWN_OPS
from factory.intake.transcribe import TranscribeError, read_source
from factory.metrics import gate3_rework, supervisor_metrics
from factory.supervisors.architecture import ArchitectureSupervisor
from factory.supervisors.model_base import ClaudeJudge
from factory.supervisors.spec_review import SpecSupervisor
from factory.task import Task

HARD_GATE_NOTE = (
    "硬闸门：不可逆操作必须由人执行。"
    "agent 只能生成待执行脚本，本管道不代为执行。"
)


def build_adapter(
    harness: str, *, binary: str, shell_argv: list[str] | None = None
):
    """按名字选 harness。审计表的 harness 字段就是这个名字。

    监工侧的 judge 始终是 claude —— 换 worker 不等于换裁判，
    否则「换个 harness 顺手把审查也换松了」会成为一条绕过验收的路径。
    """
    if harness == "claude_code":
        return ClaudeCodeAdapter(binary=binary)
    if harness == "shell":
        if not shell_argv:
            raise ValueError("--harness shell 需要 --shell-argv，例如 "
                             "--shell-argv ./codemod.py '{prompt}'")
        return ShellAdapter(shell_argv, name="shell")
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
) -> Dispatcher:
    """两个调模型的监工默认关。它们每轮都花钱，开关交给调用方。"""
    def judge() -> ClaudeJudge:
        return ClaudeJudge(
            binary=judge_binary, model=judge_model, timeout_s=timeout_s
        )

    return Dispatcher(
        adapter=build_adapter(harness, binary=binary, shell_argv=shell_argv),
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
    if report.escalation_reason:
        print(f"reason   : {report.escalation_reason}")
    if report.outcome is Outcome.BLOCKED_HARD_GATE:
        print(HARD_GATE_NOTE)


def _cmd_run(ns: argparse.Namespace) -> int:
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
    store = AuditStore(ns.db)
    if not store.exists(ns.attempt_id):
        print(f"没有 attempt id={ns.attempt_id}")
        return 1
    store.link_defect(ns.attempt_id, ns.defect_id)
    row = store.get(ns.attempt_id)
    print(f"attempt {ns.attempt_id} defects={list(row.linked_defects)}")
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
    run.add_argument("--workspace", required=True, help="目标 git 仓库")
    run.add_argument("--worktree", action="store_true",
                     help="即使只有一个任务也开独立 worktree（多任务时自动开）")
    run.add_argument("--worktree-root", default=None,
                     help="worktree 存放目录，默认仓库同级的 .factory-worktrees")
    run.add_argument("--parallel", type=int, default=1,
                     help="同时跑几个任务（默认 1，串行）")
    run.add_argument("--db", default="audit.db")
    run.add_argument("--binary", default="claude")
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--max-turns", type=int, default=None)
    run.add_argument("--spec-review", action="store_true",
                     help="开规格监工（调模型，按轮计费）")
    run.add_argument("--architecture-review", action="store_true",
                     help="开架构监工（调模型；其意见不能单独否决合并）")
    run.add_argument("--judge-model", default="sonnet",
                     help="两个调模型监工用的档位")
    run.add_argument("--harness", default="claude_code",
                     choices=("claude_code", "shell"),
                     help="worker 用哪个 harness（默认 claude_code）")
    # action="extend"：重复给 --shell-argv 要累加而不是覆盖。
    # 默认的 nargs="+" 会让后一个 flag 顶掉前一个，于是 argv[0] 变成 "{prompt}"，
    # 报出来是 "cannot launch {prompt}" —— 排查起来完全看不出是参数被吞了。
    run.add_argument("--shell-argv", nargs="+", action="extend", default=None,
                     help="--harness shell 的命令行，支持 {prompt} / {workspace} 占位符")
    run.add_argument("--judge-binary", default="claude",
                     help="监工用的 CLI。换 worker 不换裁判，所以和 --binary 分开")
    run.set_defaults(func=_cmd_run)

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
    df.add_argument("attempt_id", type=int)
    df.add_argument("defect_id")
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
