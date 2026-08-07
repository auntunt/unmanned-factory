"""跑批日志测试。

这一层要回答的是「昨晚跑得怎么样」。最要紧的一条是**崩掉的循环要能看出来**：
被 kill -9 的进程没机会写 run_end，也没机会报警 —— 不把这件事数出来的话，
一个每晚启动就崩的 cron 和一个每晚无事可做的 cron 长得一模一样。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from factory.backlog.journal import Journal, Rollup


def jr(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "log")


def seed(j: Journal, *outcomes: tuple[str, str, float]) -> None:
    j.event("run_start", queue="q")
    for task_id, outcome, cost in outcomes:
        j.event("dispatch", task_id=task_id, outcome=outcome, cost_usd=cost)
    j.event("run_end", stopped_by="队列已抽干")


# ── 写 ────────────────────────────────────────────────────────────────────

def test_events_are_one_json_object_per_line(tmp_path):
    """JSONL 而不是散文：第二天要求和、要 grep，从散文里数会数错而且不报错。"""
    j = jr(tmp_path)
    j.event("dispatch", task_id="a", outcome="merged", cost_usd=0.5)
    lines = j.path_for().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "dispatch" and rec["task_id"] == "a"
    assert rec["run_id"] == j.run_id and rec["host"] and rec["ts"] > 0


def test_files_are_split_by_day(tmp_path):
    """无人循环是常驻的，单文件一个月能到几十 MB，而「看看昨晚」是最常见的查询。"""
    j = jr(tmp_path)
    today = j.path_for()
    yesterday = j.path_for(time.time() - 26 * 3600)
    assert today != yesterday and today.parent == yesterday.parent


def test_a_write_failure_does_not_raise(tmp_path):
    """日志是观测手段，不是任务的一部分。磁盘满了不该让正在派发的循环停下来。"""
    j = Journal(tmp_path / "blocked")
    (tmp_path / "blocked").write_text("我是个文件不是目录", encoding="utf-8")
    j.event("dispatch", task_id="a")   # 不抛就算过


def test_unserializable_values_do_not_raise(tmp_path):
    """note 里塞进一个 Path 或异常对象是很容易发生的事。"""
    j = jr(tmp_path)
    j.event("dispatch", task_id="a", note=Path("/tmp/x"),
            err=ValueError("boom"))
    rec = json.loads(j.path_for().read_text(encoding="utf-8").splitlines()[0])
    assert rec["note"] == "/tmp/x" and "boom" in rec["err"]


# ── 读 ────────────────────────────────────────────────────────────────────

def test_tail_returns_newest_last(tmp_path):
    j = jr(tmp_path)
    for i in range(5):
        j.event("dispatch", task_id=f"t{i}")
    got = j.tail(limit=3)
    assert [e["task_id"] for e in got] == ["t2", "t3", "t4"]


def test_tail_can_filter_by_kind(tmp_path):
    j = jr(tmp_path)
    seed(j, ("a", "merged", 0.1))
    assert len(j.tail(limit=50, kind="dispatch")) == 1
    assert len(j.tail(limit=50, kind="run_end")) == 1


def test_tail_reads_across_day_files(tmp_path):
    """「最近 20 个」很可能横跨午夜。只读当天文件的话，凌晨一点看到的是空的。"""
    j = jr(tmp_path)
    j.dir.mkdir(parents=True)
    old = j.path_for(time.time() - 26 * 3600)
    old.write_text(json.dumps({"kind": "dispatch", "task_id": "yesterday"})
                   + "\n", encoding="utf-8")
    j.event("dispatch", task_id="today")
    assert [e["task_id"] for e in j.tail(limit=10)] == ["yesterday", "today"]


def test_a_half_written_line_is_skipped_not_fatal(tmp_path):
    """循环被 kill 时最后一行可能只写了一半。一个半行不该让 --history 整个不可用。"""
    j = jr(tmp_path)
    j.event("dispatch", task_id="good")
    with j.path_for().open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "dispatch", "task_i')
    got = j.tail(limit=10)
    assert [e["task_id"] for e in got] == ["good"]


def test_tail_on_a_missing_dir_is_empty_not_an_error(tmp_path):
    assert jr(tmp_path).tail(limit=10) == ()


# ── 汇总 ──────────────────────────────────────────────────────────────────

def test_rollup_sums_cost_and_counts_what_needs_a_human(tmp_path):
    j = jr(tmp_path)
    seed(j, ("a", "merged", 0.10), ("b", "escalated", 0.40),
         ("c", "blocked_hard_gate", 0.0), ("d", "error", 0.05))
    r = Rollup(j.tail(limit=50))
    assert r.cost_usd == 0.55
    assert {e["task_id"] for e in r.needs_human} == {"b", "c", "d"}, (
        "escalated / blocked / error 三种都要人看，是同一个「明早看几个」的数"
    )


def test_rollup_names_the_priciest_task(tmp_path):
    """一个跑失控的任务在总额里看不出来，但它就是要改的那个。"""
    j = jr(tmp_path)
    seed(j, ("cheap", "merged", 0.01), ("pricey", "escalated", 2.50),
         ("mid", "merged", 0.30))
    assert Rollup(j.tail(limit=50)).priciest["task_id"] == "pricey"


def test_rollup_priciest_is_none_when_nothing_cost_anything(tmp_path):
    """shell harness 跑出来全是 $0 —— 不能因此报一个「最贵：$0」的假信号。"""
    j = jr(tmp_path)
    seed(j, ("a", "merged", 0.0))
    r = Rollup(j.tail(limit=50))
    assert r.priciest is None
    assert not any("最贵" in ln for ln in r.lines())


def test_a_killed_loop_is_visible_as_a_crash(tmp_path):
    """被 kill -9 的进程没机会写 run_end，也没机会报警。不数出来的话，
    一个每晚启动就崩的 cron 和一个每晚无事可做的 cron 长得一模一样。"""
    j = jr(tmp_path)
    j.event("run_start", queue="q")          # 这次崩了
    crashed_id, j.run_id = j.run_id, "second-run"
    seed(j, ("a", "merged", 0.1))            # 这次正常收尾
    r = Rollup(j.tail(limit=50))
    assert r.crashed_runs == 1
    assert any("非正常退出" in ln for ln in r.lines())
    assert crashed_id != j.run_id


def test_a_clean_run_is_not_reported_as_a_crash(tmp_path):
    j = jr(tmp_path)
    seed(j, ("a", "merged", 0.1))
    r = Rollup(j.tail(limit=50))
    assert r.crashed_runs == 0
    assert not any("非正常退出" in ln for ln in r.lines())


def test_rollup_counts_recoveries(tmp_path):
    j = jr(tmp_path)
    j.event("run_start", queue="q")
    j.event("recover", task_id="orphan", reason="claim 进程 pid=999 已不在")
    j.event("run_end", stopped_by="队列已抽干")
    r = Rollup(j.tail(limit=50))
    assert len(r.recovered) == 1
    assert any("崩溃残留回收 1" in ln for ln in r.lines())


def test_rollup_truncates_a_long_needs_human_list(tmp_path):
    """待人介入 200 个的时候，把 200 行糊在终端上等于没有摘要。"""
    j = jr(tmp_path)
    seed(j, *[(f"t{i}", "escalated", 0.0) for i in range(25)])
    lines = Rollup(j.tail(limit=100)).lines()
    listed = [ln for ln in lines if ln.startswith("  [")]
    assert len(listed) == 10
    assert any("另有 15 个" in ln for ln in lines)


def test_rollup_on_an_empty_journal_says_zero_not_crash(tmp_path):
    r = Rollup(())
    assert r.cost_usd == 0.0 and r.crashed_runs == 0 and r.priciest is None
    assert "任务 0 个" in r.lines()[0]


# ── 接进循环：日志得由循环自己落盘 ────────────────────────────────────────
#
# cron 跑的循环，stdout 默认进邮件或者干脆丢掉。指望调用方记得加 `>> log`
# 等于没有日志。所以这一段测的是接线，不是 Journal 本身。

def run_loop(tmp_path: Path, dispatch, *, tasks=("a.yaml",)):
    from factory.backlog.loop import BacklogLoop, Idle, LoopLimits
    from factory.backlog.store import LOG, Backlog

    q = Backlog(tmp_path / "q").ensure()
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for name in tasks:
        f = src / name
        f.write_text("task_id: T\nprompt: p\n", encoding="utf-8")
        q.add(f)
    lp = BacklogLoop(q, dispatch, limits=LoopLimits(idle=Idle.DRAIN),
                     log=lambda _m: None)
    return q, lp, Journal(q.dir(LOG))


def test_the_loop_writes_its_own_journal_without_being_asked(tmp_path):
    """log/ 这个目录在 store 里一直被创建但从没人写过 —— 一个建好就空着的
    目录比没有这个目录更容易让人以为「日志在别处」。"""
    from factory.backlog.loop import TaskRun

    q, lp, journal = run_loop(
        tmp_path, lambda _p: TaskRun(outcome="merged", cost_usd=0.2))
    lp.run()
    kinds = [e["kind"] for e in journal.tail(limit=50)]
    assert kinds == ["run_start", "dispatch", "run_end"]
    roll = Rollup(journal.tail(limit=50))
    assert roll.cost_usd == 0.2 and roll.crashed_runs == 0


def test_run_end_is_written_even_when_the_loop_itself_raises(tmp_path):
    """走 finally。循环自己炸了也要留下收尾行，否则和被 kill 分不开 ——
    而两者要查的地方不一样（一个看 traceback，一个看 OOM / 是谁 kill 的）。"""
    from factory.backlog.store import LOG, Backlog

    q = Backlog(tmp_path / "q").ensure()
    journal = Journal(q.dir(LOG))

    class Exploding(Backlog):
        def recover(self, **kw):
            raise RuntimeError("队列目录被人删了")

    from factory.backlog.loop import BacklogLoop, Idle, LoopLimits

    lp = BacklogLoop(Exploding(q.root), lambda _p: None,
                     limits=LoopLimits(idle=Idle.DRAIN), log=lambda _m: None,
                     journal=journal)
    try:
        lp.run()
    except RuntimeError:
        pass
    kinds = [e["kind"] for e in journal.tail(limit=50)]
    assert kinds == ["run_start", "run_end"]
    assert Rollup(journal.tail(limit=50)).crashed_runs == 0


def test_a_dispatch_exception_lands_in_the_journal_as_an_error(tmp_path):
    q, lp, journal = run_loop(
        tmp_path, lambda _p: (_ for _ in ()).throw(ValueError("炸了")),
        tasks=("a.yaml",))
    lp.run()
    disp = journal.tail(limit=50, kind="dispatch")
    assert disp[0]["outcome"] == "error" and "炸了" in disp[0]["note"]
    assert len(Rollup(journal.tail(limit=50)).needs_human) == 1


def test_journal_records_per_task_wall_clock(tmp_path):
    """「哪个任务卡住了」和「哪个任务最贵」是两个问题，都要能回答。"""
    from factory.backlog.loop import TaskRun

    q, lp, journal = run_loop(tmp_path, lambda _p: TaskRun(outcome="merged"))
    lp.run()
    disp = journal.tail(limit=50, kind="dispatch")
    assert "wall_clock_s" in disp[0] and disp[0]["wall_clock_s"] >= 0


def test_recovered_orphans_show_up_in_the_journal(tmp_path):
    """崩溃残留是「昨晚出过事」最直接的证据，不能只打在终端上。"""
    import os

    from factory.backlog.loop import TaskRun
    from factory.backlog.store import LOG, Backlog

    q = Backlog(tmp_path / "q").ensure()
    src = tmp_path / "src"
    src.mkdir()
    f = src / "orphan.yaml"
    f.write_text("task_id: T\n", encoding="utf-8")
    claim = q.claim(q.add(f))
    q._claim_file(claim.path).unlink()      # 没有 .claim = 残留

    from factory.backlog.loop import BacklogLoop, Idle, LoopLimits

    journal = Journal(q.dir(LOG))
    BacklogLoop(q, lambda _p: TaskRun(outcome="merged"),
                limits=LoopLimits(idle=Idle.DRAIN), log=lambda _m: None,
                journal=journal).run()
    rec = journal.tail(limit=50, kind="recover")
    assert len(rec) == 1 and rec[0]["task_id"] == "orphan"
    assert os.path.isdir(q.dir(LOG))
