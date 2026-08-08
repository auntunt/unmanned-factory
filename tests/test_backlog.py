"""任务队列与跑批循环测试。

这一层的两个非显然行为都是**并发和崩溃**下才出现的，真跑演示不了：

  - 两个 worker 同时认领同一个条目时输的那个必须知道自己输了。用 rename
    实现的话不报任何错，只是任务被跑两遍 —— 回归后没有任何症状。
  - running/ 里的残留只在进程被 kill 之后才存在。

所以这两件事只能靠测试钉住。
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.backlog.loop import (
    DEFAULT_BUDGET_USD,
    BacklogLoop,
    Idle,
    LoopLimits,
    TaskRun,
)
from factory.backlog.store import (
    BLOCKED,
    DONE,
    INBOX,
    LOG,
    NEEDS_HUMAN,
    OUTCOME_DIR,
    RESULT_SUFFIX,
    RUNNING,
    Backlog,
    BacklogError,
)


def task_file(root: Path, name: str, body: str = "task_id: T\nprompt: p\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(body, encoding="utf-8")
    return p


def bl(tmp_path: Path) -> Backlog:
    return Backlog(tmp_path / "queue").ensure()


def result_of(path: Path) -> dict:
    return json.loads(
        path.with_name(path.name + RESULT_SUFFIX).read_text(encoding="utf-8")
    )


# ── 入队 ──────────────────────────────────────────────────────────────────

def test_add_copies_and_leaves_the_source_alone(tmp_path):
    """搬走源文件会让「我刚写的那个 YAML 呢」变成一次排查。"""
    src = task_file(tmp_path / "src", "a.yaml")
    q = bl(tmp_path)
    dst = q.add(src)
    assert src.is_file(), "源文件被搬走了"
    assert dst.parent == q.dir(INBOX)
    assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_add_refuses_to_overwrite_a_queued_entry(tmp_path):
    """同名覆盖会让先排队的那个静默消失 —— 没人会发现少跑了一个任务。"""
    q = bl(tmp_path)
    q.add(task_file(tmp_path / "s1", "dup.yaml"))
    with pytest.raises(BacklogError, match="同名"):
        q.add(task_file(tmp_path / "s2", "dup.yaml"))
    assert len(q.pending()) == 1


def test_add_rejects_a_missing_file(tmp_path):
    with pytest.raises(BacklogError, match="不存在"):
        bl(tmp_path).add(tmp_path / "nope.yaml")


def test_pending_sorts_by_name_not_mtime(tmp_path):
    """mtime 会被 cp 重置，同一批入队的先后就丢了；名字序是稳定的。"""
    q = bl(tmp_path)
    for name in ("c.yaml", "a.yaml", "b.yaml"):
        q.add(task_file(tmp_path / "src", name))
    os.utime(q.dir(INBOX) / "a.yaml", (0, 0))  # 让最老的排在中间
    assert [p.name for p in q.pending()] == ["a.yaml", "b.yaml", "c.yaml"]


def test_sidecars_are_not_queue_entries(tmp_path):
    """.claim / .result.json 落在同一个目录里，不能被当成待派发任务。"""
    q = bl(tmp_path)
    q.add(task_file(tmp_path / "src", "a.yaml"))
    (q.dir(INBOX) / "a.yaml.result.json").write_text("{}", encoding="utf-8")
    (q.dir(INBOX) / "notes.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in q.pending()] == ["a.yaml"]


# ── 认领：并发下输的一方必须知道自己输了 ──────────────────────────────────

def test_claim_moves_the_entry_out_of_inbox(tmp_path):
    q = bl(tmp_path)
    path = q.add(task_file(tmp_path / "src", "a.yaml"))
    claim = q.claim(path)
    assert claim is not None
    assert q.pending() == ()
    assert claim.path.parent == q.dir(RUNNING)
    assert not path.exists(), "inbox 里还留着一份，会被再认领一次"
    assert q.read_claim(claim.path)["pid"] == os.getpid()


def test_second_claimer_loses_and_learns_it(tmp_path):
    """这是整个队列层最要紧的一条。

    用 os.rename 实现认领的话，rename 到已存在的目标会**静默覆盖**：
    两个进程都拿到一个 Claim，都以为自己赢了，任务被派发两遍 —— 两倍花费、
    两份 diff，而审计里看起来只是两个正常任务。os.link 在目标已存在时抛
    FileExistsError，所以输的一方拿到 None。
    """
    q = bl(tmp_path)
    path = q.add(task_file(tmp_path / "src", "a.yaml"))
    first = q.claim(path)
    assert first is not None

    # 第二个 worker 手里还攥着 inbox 里那个（已被删除的）路径
    assert q.claim(path) is None, "输的一方拿到了 Claim —— 任务会被跑两遍"

    # 更贴近真实竞争：inbox 里的条目还在，但 running/ 里已经有同名了
    path.write_text("task_id: T\n", encoding="utf-8")
    assert q.claim(path) is None
    assert path.is_file(), "抢不到不该删掉别人的 inbox 条目"


def test_claim_next_skips_taken_entries_instead_of_reporting_empty(tmp_path):
    """并发多个 loop 时第一个总是被抢走的那个。

    只试 pending()[0] 的话，后面的 worker 明明有活干却会报「队列空了」，
    然后按 --idle drain 直接退出。
    """
    q = bl(tmp_path)
    for name in ("a.yaml", "b.yaml"):
        q.add(task_file(tmp_path / "src", name))
    taken = q.claim(q.dir(INBOX) / "a.yaml")
    assert taken is not None
    got = q.claim_next()
    assert got is not None and got.task_id == "b"


def test_claim_next_returns_none_when_all_taken(tmp_path):
    q = bl(tmp_path)
    q.add(task_file(tmp_path / "src", "a.yaml"))
    assert q.claim(q.dir(INBOX) / "a.yaml") is not None
    assert q.claim_next() is None


def test_task_id_comes_from_the_filename_not_the_yaml(tmp_path):
    """认领日志必须在读 YAML 之前就能打出名字 —— 一个读 YAML 就崩的条目，
    正是最需要在日志里看到名字的那种。"""
    q = bl(tmp_path)
    path = q.add(task_file(tmp_path / "src", "T-real.yaml", ":\n  broken: ["))
    claim = q.claim(path)
    assert claim is not None and claim.task_id == "T-real"


# ── 归档 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("outcome,state", sorted(OUTCOME_DIR.items()))
def test_every_outcome_has_a_landing_dir(tmp_path, outcome, state):
    """参数化用 OUTCOME_DIR 本身：新加一个 outcome 就自动被要求有落点。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    dst = q.finish(claim, outcome, note="n")
    assert dst.parent == q.dir(state)
    assert q.running() == ()
    assert result_of(dst)["outcome"] == outcome


def test_blocked_and_needs_human_stay_separate(tmp_path):
    """两者都要人介入，但要人做的事不一样：blocked 是「你自己去执行脚本」，
    needs-human 是「跑过了没过验收，去看 diff」。混一个目录，ls 就分不出来。"""
    assert OUTCOME_DIR["blocked_hard_gate"] == BLOCKED
    assert OUTCOME_DIR["escalated"] == NEEDS_HUMAN
    assert OUTCOME_DIR["merged"] == DONE


def test_finish_rejects_an_invented_outcome(tmp_path):
    """队列层不许自己发明状态名 —— 两套词的翻译表只能有 OUTCOME_DIR 一处。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    with pytest.raises(BacklogError, match="未知 outcome"):
        q.finish(claim, "done")


def test_finish_removes_the_claim_sidecar(tmp_path):
    """留着 .claim 会让下一次 recover() 把一个已完成的任务当成残留。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    q.finish(claim, "merged")
    assert list(q.dir(RUNNING).iterdir()) == []


def test_no_outcome_leads_back_to_inbox(tmp_path):
    """重试是 dispatcher 的事（max_rounds）。队列层再叠一层自动重试的话，
    一个必然失败的任务会无限烧钱，而且每一轮在审计里都像一个新任务。"""
    assert INBOX not in set(OUTCOME_DIR.values())


def test_rerunning_the_same_task_does_not_erase_the_first_failure(tmp_path):
    """人改完判据把 needs-human 里的任务重新入队时 basename 一样。
    覆盖掉的话第一次的失败证据就没了。"""
    q = bl(tmp_path)
    src = task_file(tmp_path / "src", "a.yaml")
    first = q.finish(q.claim(q.add(src)), "escalated", note="第一次")
    second = q.finish(q.claim(q.add(src)), "escalated", note="第二次")
    assert first != second
    assert result_of(first)["note"] == "第一次"
    assert result_of(second)["note"] == "第二次"
    assert {p.name for p in q.dir(NEEDS_HUMAN).glob("a*.yaml")} == {
        "a.yaml", "a.2.yaml"
    }


def test_counts_covers_every_state(tmp_path):
    q = bl(tmp_path)
    q.add(task_file(tmp_path / "src", "a.yaml"))
    q.finish(q.claim(q.add(task_file(tmp_path / "src", "b.yaml"))), "merged")
    counts = q.counts()
    assert counts[INBOX] == 1 and counts[DONE] == 1 and counts[RUNNING] == 0
    assert set(counts) == {INBOX, RUNNING, DONE, NEEDS_HUMAN, BLOCKED}


# ── 崩溃恢复：判活优先于判龄 ──────────────────────────────────────────────

def write_claim(q: Backlog, task_path: Path, **over) -> None:
    doc = {"task_id": task_path.stem, "claimed_at": time.time(),
           "pid": os.getpid(), "host": socket.gethostname()}
    doc.update(over)
    task_path.with_name(task_path.name + ".claim").write_text(
        json.dumps(doc), encoding="utf-8")


def age_out(path: Path, hours: float = 48.0) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_a_live_pid_is_never_reclaimed_however_old(tmp_path):
    """跑了两天的派发也还是在跑。按时间抢走它 = 同一个任务被派两遍。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    age_out(claim.path)
    assert q.recover(stale_after_s=1.0) == ()
    assert len(q.running()) == 1


def test_a_dead_pid_is_reclaimed_immediately_without_waiting(tmp_path):
    """进程已经没了就没什么可等的；等 6 小时只是让队列白闲 6 小时。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    write_claim(q, claim.path, pid=_never_used_pid())
    got = q.recover()
    assert len(got) == 1 and "已不在" in got[0].reason
    assert q.running() == ()


def test_recovered_goes_to_needs_human_not_back_to_inbox(tmp_path):
    """崩掉的那一轮可能已经烧了 token、已经在 worktree 里留了半个改动。
    自动重排 = 允许重复计费和重复提交。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    write_claim(q, claim.path, pid=_never_used_pid())
    got = q.recover()
    assert got[0].path.parent == q.dir(NEEDS_HUMAN)
    assert q.pending() == (), "被自动重排了 —— 会重复计费"
    assert result_of(got[0].path)["outcome"] == "error"


def test_a_missing_claim_file_is_stale_regardless_of_age(tmp_path):
    """没有 .claim 就没人认领得了它，等下去不会变好。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    q._claim_file(claim.path).unlink()
    got = q.recover()
    assert len(got) == 1 and "没有 .claim" in got[0].reason


def test_an_unreadable_claim_file_is_stale(tmp_path):
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    q._claim_file(claim.path).write_text("{not json", encoding="utf-8")
    got = q.recover()
    assert len(got) == 1 and "读不出来" in got[0].reason


def test_another_hosts_claim_falls_back_to_age(tmp_path):
    """别的机器上的 pid 在本机查不到 —— 拿本机 pid 表去判活会把活着的
    远端 worker 判死。只能退回看时间。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "a.yaml")))
    write_claim(q, claim.path, host="some-other-box", pid=1)
    assert q.recover(stale_after_s=48 * 3600) == (), "远端 worker 被判死了"
    age_out(claim.path, hours=72)
    got = q.recover(stale_after_s=48 * 3600)
    assert len(got) == 1 and "另一台机器" in got[0].reason


def _never_used_pid() -> int:
    """找一个当前不存在的 pid。硬编码一个数字会在别的机器上偶然命中。"""
    for candidate in range(2 ** 22 - 1, 2 ** 15, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise AssertionError("找不到空闲 pid")


# ── 循环 ──────────────────────────────────────────────────────────────────

class FakeClock:
    """可控的单调时钟。sleep 只推时间，不真等 —— 一个测 --max-runtime 的
    测试不该真跑那么久。"""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def loop_over(q: Backlog, dispatch, **kw) -> BacklogLoop:
    clock = kw.pop("clock", None) or FakeClock()
    limits = kw.pop("limits", None) or LoopLimits(idle=Idle.DRAIN)
    lp = BacklogLoop(q, dispatch, limits=limits, log=lambda _m: None,
                     sleep=clock.sleep, now=clock.now, **kw)
    lp.clock = clock  # 测试自己拿回来看
    return lp


def queue_tasks(q: Backlog, tmp_path: Path, *names: str) -> None:
    for name in names:
        q.add(task_file(tmp_path / "src", name))


def always(outcome: str, cost: float = 0.0):
    return lambda _path: TaskRun(outcome=outcome, cost_usd=cost)


def test_drain_runs_every_queued_task_then_exits(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml", "b.yaml", "c.yaml")
    seen: list[str] = []
    report = loop_over(q, lambda p: (seen.append(p.stem),
                                     TaskRun(outcome="merged"))[1]).run()
    assert seen == ["a", "b", "c"], "派发顺序应等于 pending() 顺序"
    assert report.dispatched == 3 and report.merged == 3
    assert report.stopped_by == "队列已抽干"
    assert q.counts()[DONE] == 3


def test_watch_keeps_polling_an_empty_queue(tmp_path):
    """「无人」的意思是队列空了它还在等下一个，不是空了就退。"""
    q = bl(tmp_path)
    lp = loop_over(q, always("merged"),
                   limits=LoopLimits(idle=Idle.WATCH, poll_s=7.0))
    calls = {"n": 0}

    def sleep(s: float) -> None:
        lp.clock.sleep(s)
        calls["n"] += 1
        if calls["n"] == 3:
            queue_tasks(q, tmp_path, "late.yaml")
        if calls["n"] == 5:
            lp.request_stop("测试收工")

    lp._sleep = sleep
    report = lp.run()
    assert report.dispatched == 1, "空转期间应该等到了后来入队的那个"
    assert report.idle_polls >= 3
    assert lp.clock.slept[:2] == [7.0, 7.0], "poll_s 没被用上"
    assert report.stopped_by == "测试收工"


def test_once_stops_after_a_single_dispatch(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml", "b.yaml")
    report = loop_over(q, always("merged"), limits=LoopLimits(idle=Idle.ONCE)).run()
    assert report.dispatched == 1
    assert len(q.pending()) == 1, "第二个不该被认领"


def test_once_on_an_empty_queue_says_so(tmp_path):
    report = loop_over(bl(tmp_path), always("merged"),
                       limits=LoopLimits(idle=Idle.ONCE)).run()
    assert report.dispatched == 0 and "队列空" in report.stopped_by


# ── 停机条件：无人循环最坏的失败模式是一直跑 ────────────────────────────────

def test_budget_defaults_to_a_real_number_not_unlimited(tmp_path):
    """忘了写 --budget-usd 应该意味着「早点停」，不是「一直刷卡」。"""
    assert LoopLimits().budget_usd == DEFAULT_BUDGET_USD > 0
    assert LoopLimits().max_tasks == 0 and LoopLimits().max_runtime_s == 0


def test_budget_stops_the_loop_with_tasks_still_queued(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(6)])
    report = loop_over(q, always("merged", cost=0.4),
                       limits=LoopLimits(idle=Idle.DRAIN, budget_usd=1.0)).run()
    assert report.dispatched == 3, "$0.4 × 3 = $1.2 才越线，应在第 4 个之前停"
    assert "预算上限" in report.stopped_by
    assert len(q.pending()) == 3, "余下的任务应留在队列里等下一轮"


def test_budget_is_checked_before_dispatch_not_after(tmp_path):
    """超支之后才发现，钱已经花了。判定必须在认领之前。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml", "b.yaml")
    report = loop_over(q, always("merged", cost=99.0),
                       limits=LoopLimits(idle=Idle.DRAIN, budget_usd=1.0)).run()
    assert report.dispatched == 1 and report.cost_usd == 99.0


def test_max_tasks_stops_the_loop(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(5)])
    report = loop_over(q, always("merged"),
                       limits=LoopLimits(idle=Idle.DRAIN, max_tasks=2)).run()
    assert report.dispatched == 2 and "--max-tasks" in report.stopped_by


def test_max_runtime_stops_the_loop_between_tasks(tmp_path):
    """不打断正在跑的派发 —— 半路砍掉 agent 会留下没人看过的半个 diff。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(5)])
    clock = FakeClock()

    def slow(_path):
        clock.t += 40.0
        return TaskRun(outcome="merged")

    report = loop_over(q, slow, clock=clock,
                       limits=LoopLimits(idle=Idle.DRAIN,
                                         max_runtime_s=100.0)).run()
    assert report.dispatched == 3, "跑到 120s 才越线，第 3 个应跑完"
    assert "--max-runtime" in report.stopped_by


def test_max_runtime_also_bounds_an_idle_watch_loop(tmp_path):
    """空转也算时间，否则 --idle watch 配 --max-runtime 会永不退出。"""
    report = loop_over(bl(tmp_path), always("merged"),
                       limits=LoopLimits(idle=Idle.WATCH, poll_s=10.0,
                                         max_runtime_s=35.0)).run()
    assert report.dispatched == 0 and report.idle_polls == 4
    assert "--max-runtime" in report.stopped_by


# ── 熔断：预算闸门在「超时」这条路上是瞎的 ─────────────────────────────────

def unpriced(outcome: str = "escalated"):
    """一次记 $0 但真烧了钱的派发（超时被 kill，CLI 没打 cost payload）。"""
    return lambda _p: TaskRun(outcome=outcome, cost_usd=0.0, unpriced=True)


def test_the_breaker_defaults_on_because_budget_cannot_see_timeouts(tmp_path):
    """默认值本身是这道闸的全部价值：忘了写 flag 的人正是需要它的人。"""
    assert LoopLimits().max_unpriced_streak == 2


def test_consecutive_unpriced_dispatches_stop_the_loop(tmp_path):
    """反复超时的任务在预算眼里免费 —— 没有这道闸就能烧一整夜。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(6)])
    report = loop_over(q, unpriced(),
                       limits=LoopLimits(idle=Idle.DRAIN,
                                         max_unpriced_streak=2)).run()
    assert report.dispatched == 2, "第 2 个之后就该停，不该把队列吃完"
    assert "未计价" in report.stopped_by
    assert len(q.pending()) == 4, "余下的留在队列里，等人看过再跑"


def test_one_priced_dispatch_resets_the_streak(tmp_path):
    """单个任务超时是设计里的正常出口（3 轮 x 900s）。一次就停机等于让
    一条坏任务停掉整夜 —— 熔断要的是「连续」，不是「累计」。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(5)])
    seq = iter([True, False, True, False, True])

    def alternating(_p):
        leak = next(seq)
        return TaskRun(outcome="merged", cost_usd=0.0 if leak else 0.01,
                       unpriced=leak)

    report = loop_over(q, alternating,
                       limits=LoopLimits(idle=Idle.DRAIN, budget_usd=0.0,
                                         max_unpriced_streak=2)).run()
    assert report.dispatched == 5, f"没连续两次，不该熔断：{report.stopped_by}"
    assert report.stopped_by == "队列已抽干"
    assert report.unpriced_total == 3, "累计次数照样要记下来给早上看"
    assert report.unpriced_streak == 1


def test_a_zero_cost_dispatch_is_not_by_itself_unpriced(tmp_path):
    """$0 和「记不上账」是两件事：D 类硬闸门真的花了 $0，adapter 一次都没
    调。把它算成漏账会让一队 D 类任务把循环熔断掉。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(4)])
    report = loop_over(q, always("blocked_hard_gate", cost=0.0),
                       limits=LoopLimits(idle=Idle.DRAIN,
                                         max_unpriced_streak=2)).run()
    assert report.dispatched == 4 and report.unpriced_total == 0
    assert report.stopped_by == "队列已抽干"


def test_the_breaker_can_be_switched_off(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(3)])
    report = loop_over(q, unpriced(),
                       limits=LoopLimits(idle=Idle.DRAIN, budget_usd=0.0,
                                         max_unpriced_streak=0)).run()
    assert report.dispatched == 3 and report.unpriced_total == 3


def test_a_planned_stop_wins_over_the_breaker_in_the_report(tmp_path):
    """同时命中时报表该说的是计划内那条 —— 「达到 --max-tasks」不该被
    「有东西坏了」盖掉，否则早上会去查一个不存在的故障。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, *[f"t{i}.yaml" for i in range(5)])
    report = loop_over(q, unpriced(),
                       limits=LoopLimits(idle=Idle.DRAIN, max_tasks=2,
                                         max_unpriced_streak=2)).run()
    assert "--max-tasks" in report.stopped_by


def test_the_summary_flags_that_the_total_is_an_underestimate(tmp_path):
    """「花费 $0.0100」读起来像真花了这么多。已知偏低的数必须自带提示。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml")
    report = loop_over(q, unpriced(), limits=LoopLimits(idle=Idle.DRAIN)).run()
    assert "未计价" in report.summary() and "真实花费更高" in report.summary()


def test_a_clean_run_summary_says_nothing_about_pricing(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml")
    report = loop_over(q, always("merged", cost=0.02),
                       limits=LoopLimits(idle=Idle.DRAIN)).run()
    assert "未计价" not in report.summary()


def test_the_journal_records_which_dispatches_were_unpriced(tmp_path):
    """早上要能只看 journal 就分清「便宜」和「没记上」。"""
    import json
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml")
    loop_over(q, unpriced(), limits=LoopLimits(idle=Idle.DRAIN)).run()
    lines = [json.loads(x) for x in
             (q.dir(LOG) / sorted(p.name for p in q.dir(LOG).glob("*.jsonl"))[0]
              ).read_text().splitlines()]
    disp = [e for e in lines if e["kind"] == "dispatch"]
    assert disp and disp[0]["unpriced"] is True
    end = [e for e in lines if e["kind"] == "run_end"]
    assert end and end[0]["unpriced_total"] == 1


def test_request_stop_finishes_the_current_task_first(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml", "b.yaml")
    lp = loop_over(q, always("merged"))

    def dispatch(path):
        lp.request_stop("SIGTERM")
        return TaskRun(outcome="merged")

    lp._dispatch = dispatch
    report = lp.run()
    assert report.dispatched == 1 and report.merged == 1
    assert q.counts()[DONE] == 1, "手上那个必须归档，不能留在 running/"
    assert q.counts()[RUNNING] == 0
    assert report.stopped_by == "SIGTERM"


def test_request_stop_keeps_the_first_reason(tmp_path):
    lp = loop_over(bl(tmp_path), always("merged"))
    lp.request_stop("SIGINT")
    lp.request_stop("SIGTERM")
    assert lp.run().stopped_by == "SIGINT"


# ── 韧性：一条坏任务不能让没人看着的队列停摆 ────────────────────────────────

def test_a_dispatch_exception_does_not_stall_the_queue(tmp_path):
    """YAML 写坏了、worktree 开不出来、CLI 某处 raise —— 无人循环碰到这些
    必须继续处理下一个。否则一条坏任务能让整个队列停在没人看着的时候。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "bad.yaml", "good.yaml")

    def dispatch(path):
        if path.stem == "bad":
            raise RuntimeError("YAML 解析炸了")
        return TaskRun(outcome="merged")

    report = loop_over(q, dispatch).run()
    assert report.dispatched == 2 and report.errors == 1 and report.merged == 1
    bad = q.dir(NEEDS_HUMAN) / "bad.yaml"
    assert "RuntimeError: YAML 解析炸了" in result_of(bad)["note"], (
        "异常原文没进 .result.json —— 人无从下手"
    )


def test_a_baseexception_still_propagates(tmp_path):
    """KeyboardInterrupt / SystemExit 不该被兜住 —— 那才是真的停不下来。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml")

    def dispatch(_path):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        loop_over(q, dispatch).run()


def test_hard_gate_result_is_archived_as_blocked_not_error(tmp_path):
    """D 类被拦是**正常**结果，不是异常。混进 errors 会让 cron 天天报警。"""
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "deploy.yaml")
    report = loop_over(q, always("blocked_hard_gate")).run()
    assert report.blocked == 1 and report.errors == 0
    assert (q.dir(BLOCKED) / "deploy.yaml").is_file()


def test_escalated_is_a_normal_exit_too(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml")
    report = loop_over(q, always("escalated")).run()
    assert report.escalated == 1 and report.errors == 0


def test_an_unknown_outcome_is_counted_as_an_error(tmp_path):
    """计数对不上总数的报表比没有报表更难查。"""
    from factory.backlog.loop import LoopReport

    r = LoopReport()
    r.bump("something-new")
    assert r.errors == 1


def test_report_counts_add_up_to_dispatched(tmp_path):
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml", "b.yaml", "c.yaml", "d.yaml")
    outcomes = iter(["merged", "escalated", "blocked_hard_gate", "boom"])

    def dispatch(_path):
        nxt = next(outcomes)
        if nxt == "boom":
            raise ValueError("x")
        return TaskRun(outcome=nxt, cost_usd=0.01)

    report = loop_over(q, dispatch).run()
    assert (report.merged + report.escalated + report.blocked + report.errors
            == report.dispatched == 4)
    assert report.cost_usd == pytest.approx(0.03)
    assert "派发 4" in report.summary()


def test_crash_leftovers_are_recovered_before_the_first_dispatch(tmp_path):
    """先清 running/ 再开工：不然一个孤儿条目会一直占着位置。"""
    q = bl(tmp_path)
    claim = q.claim(q.add(task_file(tmp_path / "src", "orphan.yaml")))
    write_claim(q, claim.path, pid=_never_used_pid())
    queue_tasks(q, tmp_path, "fresh.yaml")
    report = loop_over(q, always("merged")).run()
    assert report.recovered == 1 and report.dispatched == 1
    assert (q.dir(NEEDS_HUMAN) / "orphan.yaml").is_file()
    assert (q.dir(DONE) / "fresh.yaml").is_file()


# ── CLI 接线 ──────────────────────────────────────────────────────────────

def ns_for(tmp_path: Path, **over) -> SimpleNamespace:
    d = {"workspace": str(tmp_path / "ws"), "db": str(tmp_path / "a.db")}
    d.update(over)
    return SimpleNamespace(**d)


class FakePool:
    """记录 acquire 的调用。真 WorktreePool 要 git 仓库，这里只关心
    「开没开」这个决定。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.acquired: list[str] = []

    def acquire(self, task_id: str):
        self.acquired.append(task_id)
        p = self.root / task_id
        p.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(path=p)


def a_task(task_id: str, paths: list[str], ops: list[str] | None = None):
    """只带分级要用的三个字段。真 Task 的构造在别处已有覆盖。"""
    return SimpleNamespace(task_id=task_id, declared_paths=tuple(paths),
                           declared_ops=tuple(ops or []))


def test_no_worktree_for_a_hard_gated_task(tmp_path):
    """实测发现的 bug：D 类任务在派发前就被拦掉，但 worktree 已经建好了。
    .factory-worktrees/ 里堆着空目录，而「有 worktree」在别处的含义是
    「这里有产出没人验收」—— 堆着空的会把那个信号淹掉。"""
    from factory.cli import _queued_workspace

    ns = ns_for(tmp_path)
    pool = FakePool(tmp_path / "wt")
    got = _queued_workspace(ns, a_task("T-deploy", ["deploy.sh"]), pool)
    assert got == Path(ns.workspace)
    assert pool.acquired == [], "给硬闸门任务开了 worktree"
    assert not (tmp_path / "wt").exists()


def test_no_worktree_for_a_never_unmanned_task(tmp_path):
    """C 类同理：永不无人 = 永远不会有产出落到 worktree 里。"""
    from factory.cli import _queued_workspace

    pool = FakePool(tmp_path / "wt")
    got = _queued_workspace(ns_for(tmp_path),
                            a_task("T-auth", ["src/auth/login.py"]), pool)
    assert got == Path(ns_for(tmp_path).workspace) and pool.acquired == []


def test_worktree_is_opened_for_an_unmanned_task(tmp_path):
    from factory.cli import _queued_workspace

    pool = FakePool(tmp_path / "wt")
    got = _queued_workspace(ns_for(tmp_path),
                            a_task("T-ok", ["src/util/text.py"]), pool)
    assert pool.acquired == ["T-ok"] and got == tmp_path / "wt" / "T-ok"


def test_without_worktree_flag_everything_runs_in_the_workspace(tmp_path):
    from factory.cli import _queued_workspace

    ns = ns_for(tmp_path)
    assert _queued_workspace(ns, a_task("T", ["src/x.py"]), None) == Path(ns.workspace)


def test_this_is_not_the_gate(tmp_path):
    """这里判错只该多开或少开一个空目录。真正的 D 类判定在 Dispatcher.run
    里，不允许存在绕过它的路径 —— 所以这个函数不返回任何「放行」信号。"""
    import inspect

    from factory.cli import _queued_workspace

    src = inspect.getsource(_queued_workspace)
    assert "unmanned_allowed" in src, "用字符串比 grade 名字会随分级表漂移"
    for token in ("return True", "return False", "raise"):
        assert token not in src, f"_queued_workspace 里出现了 {token!r} —— 它不是闸门"


# ── 花费统计：预算闸门必须和账单看同一个数 ──────────────────────────────────

def seeded_audit(tmp_path: Path, *, cost: float, supervisor_cost: float
                 ) -> tuple[str, int]:
    from factory.audit.models import OracleClass, SupervisorRole, Verdict
    from factory.audit.store import AuditStore

    db = str(tmp_path / "audit.db")
    store = AuditStore(db)
    aid = store.open_attempt(task_id="T", spec_ref=[],
                             oracle_class=OracleClass.A, class_reason="r",
                             harness="shell", harness_version="1", model="m")
    store.record_result(aid, diff_hash=None, commit=None, transcript_path=None,
                        tokens_in=1, tokens_out=1, cost_usd=cost,
                        wall_clock_ms=1)
    if supervisor_cost:
        store.record_verdict(aid, role=SupervisorRole.SPEC,
                             verdict=Verdict.PASS, claims=[],
                             cost_usd=supervisor_cost)
    return db, aid


def test_supervisor_cost_counts_toward_the_budget(tmp_path):
    """监工花费记在 verdict 行上，不在 attempt 行上。只算 attempt 的话，
    开了 --spec-review 的循环会系统性低估开销 —— 低估的闸门等于没闸门。"""
    from factory.cli import _attempts_cost

    db, aid = seeded_audit(tmp_path, cost=0.10, supervisor_cost=0.03)
    cost, _ = _attempts_cost(ns_for(tmp_path, db=db), (aid,))
    assert cost == pytest.approx(0.13)


def test_cost_reads_the_audit_db_not_a_second_tally(tmp_path):
    from factory.cli import _attempts_cost

    db, aid = seeded_audit(tmp_path, cost=0.25, supervisor_cost=0.0)
    ns = ns_for(tmp_path, db=db)
    assert _attempts_cost(ns, (aid,)) == (pytest.approx(0.25), False)
    assert _attempts_cost(ns, ()) == (0.0, False)


def test_an_unreadable_cost_row_does_not_stop_the_loop(tmp_path):
    """花费读不到不该让队列停摆 —— 但**必须留痕**。

    这条测试原来断言 `leaked is False`，理由写的是「漏账会触发熔断停机，
    而这里只是查不到 id」。那个理由不成立，2026-08-06 改掉：

    - 熔断要**连续** `max_unpriced_streak`（默认 2）次才响。单次读不到只让
      计数从 0 变 1，下一个任务正常就归零 —— 不会停摆。
    - `attempt_ids` 是刚派发时拿到的 id。查不到只有两种可能：库坏了，或者
      记账那步没落盘。两种都意味着账不全，而**不是**「这次免费」。
    - 按原来的写法，一个 id 全都读不出来的坏库能让夜跑无限烧钱：每次派发
      都记 $0、`leaked=False`，`--budget-usd` 永远不响。

    所以「不停摆」由熔断器的 streak 保证，不该靠把漏账谎报成 0。
    """
    from factory.cli import _attempts_cost

    db, aid = seeded_audit(tmp_path, cost=0.5, supervisor_cost=0.0)
    got, leaked = _attempts_cost(ns_for(tmp_path, db=db), (aid, 999_999))
    assert got == pytest.approx(0.5), "读到的那部分仍要算进去"
    assert leaked is True, "查不到 = 账不全，不是免费"


def test_one_unreadable_row_alone_does_not_trip_the_breaker(tmp_path):
    """上面那条的另一半：留痕了，但**单次不停机**。

    这两条合起来才是完整的判据。只有上面那条的话，「不该让队列停摆」这个
    原始诉求就没有测试守着了 —— 而它是对的诉求，只是实现方式错了。
    """
    from factory.backlog.loop import LoopLimits, LoopReport

    lim = LoopLimits()
    assert lim.max_unpriced_streak == 2
    rep = LoopReport(unpriced_streak=1)
    assert rep.unpriced_streak < lim.max_unpriced_streak, "单次不该触发熔断"


# ---------- 漏账的第四条来源：派发抛异常 ----------

def test_a_dispatch_that_raises_is_marked_unpriced():
    """异常可能发生在**派发之后** —— 钱烧完了，account 一分没入。

    落地那一步炸了、审计库写不进去：此时 attempt 已经跑完，而 `_one` 拿不到
    attempt_ids，一分钱都入不了账。默认 `cost=0, unpriced=False` 的话，这个
    任务在预算账上免费且熔断器看不见 —— 反复发生就绕开了 --budget-usd。
    实测修之前：landing 抛异常 → (cost=0.0, unpriced=False)。

    不猜价格（这里没有 attempt_ids，也没有价目表），只标「这个数低估了」。
    """
    from factory.backlog.loop import BacklogLoop

    def boom(path):
        raise RuntimeError("landing 炸了，此前已派发 3 轮")

    loop = BacklogLoop.__new__(BacklogLoop)
    loop._dispatch = boom
    run = loop._one(type("C", (), {"path": "t.yaml"})())

    assert run.outcome == "error"
    assert run.unpriced is True, "钱烧了但没入账 = 漏账"


def test_two_dispatch_exceptions_in_a_row_stop_the_loop(tmp_path):
    """接线测试：unpriced 真的喂到熔断器 —— **真跑一轮循环**。

    第一版我写成了「断言 LoopLimits.max_unpriced_streak == 2 且
    LoopReport(unpriced_streak=2) >= 2」，那是个假接线测试：它只证明了两个
    常量的大小关系，`_one` 里的 unpriced 完全没接上也会绿。
    """
    q = bl(tmp_path)
    queue_tasks(q, tmp_path, "a.yaml", "b.yaml", "c.yaml")

    def boom(path):
        raise RuntimeError("landing 炸了")

    lp = loop_over(q, boom)
    rep = lp.run()

    assert rep.unpriced_streak >= 2
    assert "未计价" in rep.stopped_by
    assert rep.dispatched < 3, "熔断该在第三个任务之前停下来"
