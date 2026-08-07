"""无人工厂入口。

    run       派发一个任务
    show      打印某个任务的审计轨迹
    override  人工定案 resolution（spec §5：此字段事后填写）
    defect    事后挂 defect，捕获漏报
    metrics   spec §5.1 的三个数，用于两周后裁剪监工
"""
from __future__ import annotations

import argparse
from pathlib import Path

from factory.audit.models import Resolution
from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.harness.base import Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.metrics import gate3_rework, supervisor_metrics
from factory.task import Task

HARD_GATE_NOTE = (
    "硬闸门：不可逆操作必须由人执行。"
    "agent 只能生成待执行脚本，本管道不代为执行。"
)


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


def _cmd_run(ns: argparse.Namespace) -> int:
    task = Task.from_yaml(ns.task)
    dispatcher = build_dispatcher(
        ns.db, binary=ns.binary, timeout_s=ns.timeout, max_turns=ns.max_turns
    )
    report = dispatcher.run(task, Path(ns.workspace))

    print(f"task     : {task.task_id}")
    print(f"outcome  : {report.outcome.value}")
    print(f"rounds   : {report.rounds}")
    print(f"class    : {report.final_grade.oracle_class.value}"
          f"  ({report.final_grade.reason})")
    print(f"attempts : {list(report.attempt_ids)}")
    if report.escalation_reason:
        print(f"reason   : {report.escalation_reason}")
    if report.outcome is Outcome.BLOCKED_HARD_GATE:
        print(HARD_GATE_NOTE)
    return 0 if report.outcome is Outcome.MERGED else 1


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
              f"未定案 {m.unadjudicated} 次")
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

    run = sub.add_parser("run", help="派发一个任务")
    run.add_argument("task", help="任务 YAML 路径")
    run.add_argument("--workspace", required=True, help="目标 git 仓库")
    run.add_argument("--db", default="audit.db")
    run.add_argument("--binary", default="claude")
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--max-turns", type=int, default=None)
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
