"""队列层真依赖：前置没合并（不在 done/）就不认领。

这一组测试盯的两个失败方向，都是「看起来正常」的那种：

  1. 判据写成「不在 inbox 里」而不是「在 done 里」。needs-human 和 blocked
     里的前置会被算成满足 —— 那正是最该先让人看一眼的两种状态，而认领成功
     在日志里和依赖满足长得完全一样。
  2. 死锁条目留在 inbox 只打日志。一个永不被认领的条目和一个空队列，在
     counts()、在 pending() 长度、在循环报表上全都一样 —— 整夜没跑任何
     东西会显示成「队列已抽干」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from factory.backlog.store import (BLOCKED, DONE, INBOX, NEEDS_HUMAN, RUNNING,
                                   Backlog)
from factory.task import Task


def _yaml(tmp_path: Path, name: str, *, depends_on=(), extra=None) -> Path:
    doc = {"task_id": name, "prompt": f"做 {name}"}
    if depends_on:
        doc["depends_on"] = list(depends_on)
    if extra:
        doc.update(extra)
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return p


def _bl(tmp_path: Path) -> Backlog:
    return Backlog(tmp_path / "queue").ensure()


def _place(bl: Backlog, src: Path, state: str) -> Path:
    """直接把一份 YAML 放进某个状态目录（绕过 add，用来摆前置的现状）。"""
    dst = bl.dir(state) / src.name
    dst.write_bytes(src.read_bytes())
    return dst


def _place_running(bl: Backlog, src: Path) -> Path:
    """摆一个**真的在跑**的条目：带 .claim，pid 是本进程。

    光把 YAML 丢进 running/ 不够 —— 没有 .claim 的条目按设计就是崩溃残留，
    循环开头的 recover() 会立刻把它搬去 needs-human，于是「前置在跑」的测试
    实际测的是「前置在 needs-human」。第一版就是这么写错的。
    """
    import json
    import os
    import socket
    import time
    dst = _place(bl, src, RUNNING)
    dst.with_name(dst.name + ".claim").write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                    "claimed_at": time.time()}),
        encoding="utf-8")
    return dst


# ---------- 字段 ----------

def test_task_reads_depends_on(tmp_path: Path) -> None:
    p = _yaml(tmp_path, "T-b", depends_on=["T-a"])
    assert Task.from_yaml(p).depends_on == ("T-a",)


def test_task_without_depends_on_is_empty(tmp_path: Path) -> None:
    assert Task.from_yaml(_yaml(tmp_path, "T-a")).depends_on == ()


# ---------- 满足判据 ----------

def test_dep_in_done_is_claimable(tmp_path: Path) -> None:
    bl = _bl(tmp_path)
    _place(bl, _yaml(tmp_path, "T-a"), DONE)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    claim = bl.claim_next()
    assert claim is not None and claim.task_id == "T-b"


def test_dep_in_inbox_is_not_claimable(tmp_path: Path) -> None:
    bl = _bl(tmp_path)
    # 只有 T-b 排在 inbox，它的前置 T-a 还没入队。
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    assert bl.claim_next() is None


@pytest.mark.parametrize("state", [NEEDS_HUMAN, BLOCKED])
def test_dep_parked_for_human_is_not_claimable(tmp_path: Path, state: str) -> None:
    """这条最容易写反。

    前置在 needs-human = 跑过了但没过验收；在 blocked = D 类永不无人跑。
    两种都**不算合并**。把判据写成「不在 inbox 里」的实现会让这两条过关，
    于是后继去改一片刚失败的代码，而报表上是一次正常派发。
    """
    bl = _bl(tmp_path)
    _place(bl, _yaml(tmp_path, "T-a"), state)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    assert bl.claim_next() is None
    assert bl.missing_deps(bl.pending()[0]) == ("T-a",)


def test_dep_in_running_is_not_claimable_yet(tmp_path: Path) -> None:
    """正在跑 ≠ 已合并。它可能失败。"""
    bl = _bl(tmp_path)
    _place_running(bl, _yaml(tmp_path, "T-a"))
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    assert bl.claim_next() is None


def test_park_suffix_still_satisfies(tmp_path: Path) -> None:
    """`_park` 撞名时加的 `.N` 后缀不许让依赖失效。

    同一个任务跑第二遍（人改完判据重新 add）会落成 T-a.2.yaml。不剥后缀的
    实现会让所有后继永远等下去，而 done/ 里明明躺着那个前置。
    """
    bl = _bl(tmp_path)
    src = _yaml(tmp_path, "T-a")
    dst = bl.dir(DONE) / "T-a.2.yaml"
    dst.write_bytes(src.read_bytes())
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    assert "T-a" in bl.merged_ids()
    claim = bl.claim_next()
    assert claim is not None and claim.task_id == "T-b"


def test_unreadable_yaml_is_still_claimable(tmp_path: Path) -> None:
    """坏 YAML 不在这里扣下。

    该由 dispatcher 报错并归 needs-human。在认领处悄悄跳过它，会造出一个
    永不派发、报表上又看不出来的条目。
    """
    bl = _bl(tmp_path)
    bad = tmp_path / "T-bad.yaml"
    bad.write_text("prompt: [未闭合\n", encoding="utf-8")
    bl.add(bad)
    claim = bl.claim_next()
    assert claim is not None and claim.task_id == "T-bad"


# ---------- 跳过而不是停下 ----------

def test_blocked_entry_does_not_stall_the_one_behind_it(tmp_path: Path) -> None:
    """名字序在前的被挡住时，后面能跑的照样跑。

    实现成「第一个不可认领就返回 None」的话，一个等前置的任务会把整个队列
    堵死，而日志上写的是「队列空」。
    """
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-aaa", depends_on=["T-zzz"]))
    bl.add(_yaml(tmp_path, "T-bbb"))
    claim = bl.claim_next()
    assert claim is not None and claim.task_id == "T-bbb"
    # 被挡住的那个还在 inbox，没被认领也没被搬走。
    assert [p.name for p in bl.pending()] == ["T-aaa.yaml"]


def test_no_deps_keeps_name_order_and_queue_jumping(tmp_path: Path) -> None:
    """无依赖时行为完全不变：仍按名字序，`00-` 仍能插队。"""
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-mmm"))
    bl.add(_yaml(tmp_path, "T-aaa"), name="00-urgent.yaml")
    claim = bl.claim_next()
    assert claim is not None and claim.name == "00-urgent.yaml"


def test_blocked_by_deps_reports_what_claim_next_skipped(tmp_path: Path) -> None:
    """两边必须是同一个判据。

    分头实现会让「跳过的」和「报出来的」对不上：循环于是拿着一个空的
    waiting 列表走进「队列已抽干」，而 inbox 里还躺着两条。
    """
    bl = _bl(tmp_path)
    _place(bl, _yaml(tmp_path, "T-a"), DONE)
    bl.add(_yaml(tmp_path, "T-ok", depends_on=["T-a"]))
    bl.add(_yaml(tmp_path, "T-w1", depends_on=["T-nope"]))
    bl.add(_yaml(tmp_path, "T-w2", depends_on=["T-nope"]))

    # 非空基线：这里如果基线是 0 条待命，「跳过了」和「一条都没读」分不出来。
    before = bl.blocked_by_deps()
    assert [p.stem for p, _ in before] == ["T-w1", "T-w2"]

    claim = bl.claim_next()
    assert claim is not None and claim.task_id == "T-ok"
    after = bl.blocked_by_deps()
    assert [p.stem for p, _ in after] == ["T-w1", "T-w2"]
    assert all(missing == ("T-nope",) for _, missing in after)


# ---------- 死锁 ----------

def test_missing_prereq_is_a_deadlock_and_gets_moved(tmp_path: Path) -> None:
    """前置根本不在队列里 → 搬去 needs-human，不是留在 inbox。"""
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-ghost"]))
    dead = bl.park_deadlocked()
    assert [d.task_id for d in dead] == ["T-b"]
    assert dead[0].missing == ("T-ghost",)
    assert bl.pending() == ()
    assert [p.name for p in bl._entries(bl.dir(NEEDS_HUMAN))] == ["T-b.yaml"]
    # 返回的 path 指向搬完之后的位置，否则调用方读它会报「文件不存在」。
    assert dead[0].path.is_file()
    assert dead[0].path.parent.name == NEEDS_HUMAN


def test_deadlock_result_json_names_the_missing_prereq(tmp_path: Path) -> None:
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-ghost"]))
    bl.park_deadlocked()
    import json
    doc = json.loads(
        (bl.dir(NEEDS_HUMAN) / "T-b.yaml.result.json").read_text("utf-8"))
    assert doc["outcome"] == "error"
    assert doc["missing_deps"] == ["T-ghost"]
    assert "T-ghost" in doc["note"]
    assert "根本没有" in doc["note"]


def test_cycle_parks_both_and_says_so(tmp_path: Path) -> None:
    """A↔B。

    「缺 B」和「缺 B，而 B 自己也在等别人」要人做的事不同：前者补一个任务，
    后者拆一个环。note 必须把这个区别写出来。
    """
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-a", depends_on=["T-b"]))
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    dead = bl.park_deadlocked()
    assert sorted(d.task_id for d in dead) == ["T-a", "T-b"]
    assert bl.pending() == ()
    assert all("成环" in d.reason for d in dead)


def test_chain_is_not_reported_as_a_cycle(tmp_path: Path) -> None:
    """链上没有环，就不许写「成环」。

    含糊成「可能成环」会让人先去找一个不存在的环。C→B→A(needs-human) 里
    C 该被告知「B 自己也等不到」，顺着链往上查。
    """
    bl = _bl(tmp_path)
    _place(bl, _yaml(tmp_path, "T-a"), NEEDS_HUMAN)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    bl.add(_yaml(tmp_path, "T-c", depends_on=["T-b"]))
    by_id = {d.task_id: d.reason for d in bl.deadlocked()}
    assert "成环" not in by_id["T-c"]
    assert "它自己也等不到" in by_id["T-c"]


def test_running_prereq_is_not_a_deadlock(tmp_path: Path) -> None:
    """前置正在跑 → 等，不判死锁。

    早一秒判死锁就会把一个马上要合并的前置的后继误杀。它失败的话就不在
    running/ 了，下一轮扫描自然抓到。
    """
    bl = _bl(tmp_path)
    _place_running(bl, _yaml(tmp_path, "T-a"))
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    assert bl.deadlocked() == ()
    assert bl.park_deadlocked() == ()
    assert [p.name for p in bl.pending()] == ["T-b.yaml"]


def test_chain_behind_a_running_task_is_not_a_deadlock(tmp_path: Path) -> None:
    """C 依赖 B、B 依赖正在跑的 A。C 也不该被判死锁 —— 它是可达的。"""
    bl = _bl(tmp_path)
    _place_running(bl, _yaml(tmp_path, "T-a"))
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    bl.add(_yaml(tmp_path, "T-c", depends_on=["T-b"]))
    assert bl.deadlocked() == ()


def test_chain_behind_a_parked_task_is_all_deadlocked(tmp_path: Path) -> None:
    """A 在 needs-human，B 依赖 A，C 依赖 B → 三个都等不到（B、C 两条）。"""
    bl = _bl(tmp_path)
    _place(bl, _yaml(tmp_path, "T-a"), NEEDS_HUMAN)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    bl.add(_yaml(tmp_path, "T-c", depends_on=["T-b"]))
    dead = bl.park_deadlocked()
    assert sorted(d.task_id for d in dead) == ["T-b", "T-c"]
    by_id = {d.task_id: d for d in dead}
    assert "没过验收" in by_id["T-b"].reason
    assert "它自己也等不到" in by_id["T-c"].reason


def test_satisfiable_entries_are_untouched_by_park(tmp_path: Path) -> None:
    """非空基线：有能跑的、有死锁的，只搬死锁那些。"""
    bl = _bl(tmp_path)
    _place(bl, _yaml(tmp_path, "T-a"), DONE)
    bl.add(_yaml(tmp_path, "T-ok", depends_on=["T-a"]))
    bl.add(_yaml(tmp_path, "T-dead", depends_on=["T-ghost"]))
    assert len(bl.pending()) == 2
    dead = bl.park_deadlocked()
    assert [d.task_id for d in dead] == ["T-dead"]
    assert [p.name for p in bl.pending()] == ["T-ok.yaml"]


# ---------- 循环 ----------

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s
        if len(self.slept) > 3:      # WATCH 模式不会自己停，测试来打断
            raise KeyboardInterrupt


def _loop(bl: Backlog, outcome: str = "merged", *, idle):
    from factory.backlog.loop import BacklogLoop, LoopLimits, TaskRun
    clock = _Clock()
    lp = BacklogLoop(bl, lambda _p: TaskRun(outcome=outcome, cost_usd=0.0),
                     limits=LoopLimits(idle=idle, poll_s=3.0),
                     log=lambda _m: None, sleep=clock.sleep, now=clock.now)
    lp.clock = clock
    return lp


def test_waiting_on_deps_is_not_reported_as_drained(tmp_path: Path) -> None:
    """这条是这一层的要害。

    「有活但等前置」被算进「队列已抽干」的话，一整夜什么都没跑会显示成
    正常收工 —— 空表和干净的表长得一样，这个仓库反复踩的形状。
    """
    from factory.backlog.loop import Idle
    bl = _bl(tmp_path)
    _place_running(bl, _yaml(tmp_path, "T-a"))   # 前置在跑，不算死锁
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    report = _loop(bl, idle=Idle.DRAIN).run()
    assert report.dispatched == 0
    assert "等前置" in report.stopped_by
    assert "抽干" not in report.stopped_by


def test_empty_queue_still_says_drained(tmp_path: Path) -> None:
    """非空基线的另一半：真空队列的措辞不许被上面那条改掉。"""
    from factory.backlog.loop import Idle
    bl = _bl(tmp_path)
    report = _loop(bl, idle=Idle.DRAIN).run()
    assert "抽干" in report.stopped_by


def test_deadlock_is_parked_by_the_loop_and_journaled(tmp_path: Path) -> None:
    from factory.backlog.loop import Idle
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-ghost"]))
    report = _loop(bl, idle=Idle.DRAIN).run()
    assert report.dep_deadlocked == 1
    # 死锁清空了 inbox，于是这一轮的结论是「队列空」而不是「等前置」——
    # 那些条目已经在 needs-human 里等人了。
    assert "抽干" in report.stopped_by
    assert bl.pending() == ()
    assert report.dep_deadlocked and "等不到前置" in report.summary()

    from factory.backlog.journal import Journal
    from factory.backlog.store import LOG
    kinds = [e["kind"] for e in Journal(bl.dir(LOG)).tail(limit=50)]
    assert "dep_deadlock" in kinds


def test_summary_omits_deadlock_when_there_is_none(tmp_path: Path) -> None:
    """0 条死锁时不许在总结里留一句「另有 0 条」—— 但计数本身要在
    report 上取得到，不是靠字符串。"""
    from factory.backlog.loop import Idle
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-a"))
    report = _loop(bl, idle=Idle.DRAIN).run()
    assert report.dispatched == 1 and report.dep_deadlocked == 0
    assert "等不到前置" not in report.summary()


def test_watch_keeps_polling_while_prereq_runs(tmp_path: Path) -> None:
    """WATCH 模式下等前置就该 poll，不是退出。"""
    from factory.backlog.loop import Idle
    bl = _bl(tmp_path)
    _place_running(bl, _yaml(tmp_path, "T-a"))
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-a"]))
    lp = _loop(bl, idle=Idle.WATCH)
    with pytest.raises(KeyboardInterrupt):
        lp.run()
    assert lp.clock.slept == [3.0, 3.0, 3.0, 3.0]
    assert [p.name for p in bl.pending()] == ["T-b.yaml"]


def test_dependency_order_is_actually_respected_end_to_end(tmp_path: Path
                                                           ) -> None:
    """两条任务，B 依赖 A，名字序故意反着（B 排前面）。

    第一轮只该跑 A；A 合并后第二轮才跑 B。这条测的是「跳过」和「后来能跑」
    合起来是对的 —— 只测跳过的话，一个永远跳过的实现也是绿的。
    """
    from factory.backlog.loop import Idle
    bl = _bl(tmp_path)
    bl.add(_yaml(tmp_path, "T-a"), name="T-zzz-a.yaml")
    bl.add(_yaml(tmp_path, "T-b", depends_on=["T-zzz-a"]), name="T-aaa-b.yaml")

    order: list[str] = []
    from factory.backlog.loop import BacklogLoop, LoopLimits, TaskRun

    def dispatch(path: Path) -> TaskRun:
        order.append(path.stem)
        return TaskRun(outcome="merged", cost_usd=0.0)

    clock = _Clock()
    BacklogLoop(bl, dispatch, limits=LoopLimits(idle=Idle.DRAIN),
                log=lambda _m: None, sleep=clock.sleep, now=clock.now).run()
    assert order == ["T-zzz-a", "T-aaa-b"]
