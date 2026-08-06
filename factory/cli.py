"""无人工厂 P0 入口：run 派发一个任务，show 打印审计轨迹。"""
from __future__ import annotations

import argparse
from pathlib import Path

from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.harness.base import Limits
from factory.harness.claude_code import ClaudeCodeAdapter
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

    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
