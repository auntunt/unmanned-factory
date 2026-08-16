"""人时账的口径。

三个要答的问题：闸门 1 花了多少人时、闸门 3 花了多少人时、一个需求从草稿到
上线的墙钟和人时怎么分布。这个文件钉的是「算错了会看不出来」的那几处：
0 和没数据必须分开、还在等人的不许当 0、没配对的端点不许静默消失。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from factory.audit.models import (
    HumanAction,
    HumanGate,
    OracleClass,
    Resolution,
    utc_now,
)
from factory.audit.store import AuditStore
from factory.metrics import human_time

MIN = timedelta(minutes=1)


@pytest.fixture
def store(tmp_path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


def _blocked(store, task_id, at):
    store.record_human_event(task_id=task_id, gate=HumanGate.INTAKE,
                             action=HumanAction.BLOCKED, created_at=at)


def _confirm(store, task_id, at):
    store.record_human_event(task_id=task_id, gate=HumanGate.INTAKE,
                             action=HumanAction.CONFIRM, created_at=at)


def _override_event(store, task_id, at):
    store.record_human_event(task_id=task_id, gate=HumanGate.DELIVERY,
                             action=HumanAction.OVERRIDE, created_at=at)


def _attempt(store, task_id="T-1") -> int:
    return store.open_attempt(
        task_id=task_id, spec_ref=[], oracle_class=OracleClass.C,
        class_reason="C: 没有廉价裁判", harness="claude_code",
        harness_version="2.1.223", model="haiku",
    )


# ---------- 没数据 ≠ 0 ----------

def test_an_empty_store_yields_none_not_zero(store):
    """全 0 会让「还没攒到数据」和「人一分钟没花」长得一样。前者该继续攒，
    后者说明这套东西已经无人了 —— 两个结论差得远。"""
    led = human_time(store)
    assert led.tasks == ()
    assert led.gate1_mean is None
    assert led.gate3_mean is None
    assert led.human_total is None
    assert led.wall_clock_mean is None


def test_a_task_still_waiting_on_a_human_is_pending_not_zero(store):
    """只有 blocked 没有 confirm = 人还没动手。算成 0 分钟会让积压看着像高效。"""
    _blocked(store, "T-1", utc_now())
    (t,) = human_time(store).tasks
    assert t.gate1_seconds is None
    assert t.gate1_pending is True
    assert human_time(store).gate1_mean is None


# ---------- 闸门 1：confirm − blocked ----------

def test_gate1_is_the_gap_between_blocked_and_confirm(store):
    t0 = utc_now()
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 7 * MIN)
    (t,) = human_time(store).tasks
    assert t.gate1_seconds == pytest.approx(420.0)
    assert t.gate1_pending is False


def test_gate1_sums_across_rounds(store):
    """一份草稿可以被拦两次（人补了一半又被拦）。只算最后一段会低估人时。"""
    t0 = utc_now()
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 2 * MIN)
    _blocked(store, "T-1", t0 + 30 * MIN)
    _confirm(store, "T-1", t0 + 33 * MIN)
    (t,) = human_time(store).tasks
    assert t.gate1_seconds == pytest.approx(300.0), "两段 2 + 3 分钟"


def test_gate1_does_not_count_the_wait_between_rounds(store):
    """人时是人花的时间，不是需求在系统里躺的时间。躺着的那段归墙钟。"""
    t0 = utc_now()
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 1 * MIN)
    _blocked(store, "T-1", t0 + timedelta(days=3))
    _confirm(store, "T-1", t0 + timedelta(days=3, minutes=1))
    (t,) = human_time(store).tasks
    assert t.gate1_seconds == pytest.approx(120.0)


def test_a_confirm_without_a_blocked_is_not_counted(store):
    """人直接把手写 YAML 塞进 needs-human 再入队，就会只有 confirm。
    拿它和「什么时候」相减都是编数 —— 宁可标成对不上，别给个看着正常的数。"""
    _confirm(store, "T-1", utc_now())
    led = human_time(store)
    (t,) = led.tasks
    assert t.gate1_seconds is None
    assert t.gate1_pending is False
    assert led.unpaired_events == 1, "对不上的端点必须看得见，不许静默丢掉"


# ---------- 积压等了多久 ----------

def test_a_waiting_task_reports_how_long_it_has_been_waiting(store):
    """在等人 = 一个布尔值不够用：等 3 天和等 1 分钟得分得开。

    这张表答的是「下一步该往哪投工」。只说「有 3 个在等」的话，一个刚判完的
    队列和一个积了一周的队列读数一样，那个数就没法用来排优先级。
    """
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    r = store.get(aid).resolved_at
    (t,) = human_time(store, now=r + timedelta(days=3)).tasks
    assert t.gate3_pending is True
    assert t.waiting_seconds == pytest.approx(3 * 86400.0)


def test_gate1_waiting_is_measured_from_the_open_blocked(store):
    """闸门 1 那边等的是「草稿被拦下之后还没人来补」。"""
    t0 = utc_now() - timedelta(hours=6)
    _blocked(store, "T-1", t0)
    (t,) = human_time(store, now=t0 + timedelta(hours=6)).tasks
    assert t.gate1_pending is True
    assert t.waiting_seconds == pytest.approx(6 * 3600.0)


def test_a_task_stuck_at_both_gates_counts_from_the_earlier_one(store):
    """两道闸门同时卡着：等待时长从**先**卡住那一刻算。

    这个任务是一份第一轮被打回、人补了一半又搁下、同时上一轮判决还挂着人的
    草稿。取更晚那个端点会把已经卡了三天的活报成刚卡一小时，于是最该先动的
    那个在排序里沉到下面。
    """
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    r = store.get(aid).resolved_at
    # 闸门 1 先卡住（三天前有人拦下没补），闸门 3 后卡住（判决刚落）。
    _blocked(store, "T-1", r - timedelta(days=3))

    (t,) = human_time(store, now=r + 1 * MIN).tasks
    assert (t.gate1_pending, t.gate3_pending) == (True, True)
    assert t.waiting_seconds == pytest.approx(3 * 86400.0 + 60.0), "从先卡住那刻算"


def test_a_task_not_waiting_has_no_waiting_time(store):
    """没在等的任务不报等待时长 —— 报 0 会混进「刚刚才进队列」里。"""
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    _override_event(store, "T-1", store.get(aid).resolved_at + 5 * MIN)
    (t,) = human_time(store).tasks
    assert t.gate3_pending is False
    assert t.waiting_seconds is None


def test_the_ledger_reports_the_longest_wait(store):
    """积压里最久的那个是该先动的。合计没用 —— 10 个各等 1 分钟不是急事。"""
    old = _attempt(store, "T-old")
    store.finalize(old, Resolution.ESCALATED)
    r = store.get(old).resolved_at
    _blocked(store, "T-fresh", r + timedelta(days=2))
    led = human_time(store, now=r + timedelta(days=2, minutes=5))
    assert led.longest_wait_seconds == pytest.approx(2 * 86400.0 + 300.0)
    assert led.longest_waiting_task == "T-old"


def test_no_backlog_means_no_longest_wait(store):
    """空积压报 None，不报 0 —— 和这个文件里其他分母一个规矩。"""
    store.finalize(_attempt(store), Resolution.MERGED)
    led = human_time(store)
    assert led.longest_wait_seconds is None
    assert led.longest_waiting_task is None


def test_waiting_time_is_not_added_into_human_time(store):
    """等待时长不是人时。人还没来看，这段时间没人在花。

    混进去的话「人时占比」会随着积压变久一路涨到 100% 以上，而那个数是拿来
    证明「无人」程度的。
    """
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    r = store.get(aid).resolved_at
    _blocked(store, "T-1", r - 10 * MIN)
    _confirm(store, "T-1", r - 8 * MIN)
    led = human_time(store, now=r + timedelta(days=1))
    (t,) = led.tasks
    assert t.human_seconds == pytest.approx(120.0, abs=2.0), "只有闸门 1 那 2 分钟"
    assert led.human_total == pytest.approx(120.0, abs=2.0)


# ---------- 闸门 3：override − resolved_at ----------

def test_gate3_starts_at_resolved_at_not_at_dispatch(store):
    """判决落下才是「开始等人」。从派发时刻算会把 worker 干活的时间算成人时。"""
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    resolved = store.get(aid).resolved_at
    _override_event(store, "T-1", resolved + 9 * MIN)
    (t,) = human_time(store).tasks
    assert t.gate3_seconds == pytest.approx(540.0, abs=2.0)


def test_gate3_is_pending_while_nobody_has_ruled(store):
    """判完了没人来 = 积压。算 0 会让「等验收的一堆」看着像已经验收完。"""
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    (t,) = human_time(store).tasks
    assert t.gate3_seconds is None
    assert t.gate3_pending is True


def test_an_attempt_never_resolved_is_not_pending_on_a_human(store):
    """还在跑的 attempt 不算「等人」—— 那样积压数会把在跑的也算进去。"""
    _attempt(store)
    (t,) = human_time(store).tasks
    assert t.gate3_seconds is None
    assert t.gate3_pending is False


def test_an_auto_landed_task_is_not_waiting_on_a_human(store):
    """全绿自动合并 = 人一分钟没花，不是「等人验收」。

    「有判决、没 override」不足以判定在等人：自动落地的任务永远长这样，而它
    恰好是这套系统的**常态**。只看这两样，工厂跑得越顺，假积压就越大。
    """
    store.finalize(_attempt(store), Resolution.MERGED)
    (t,) = human_time(store).tasks
    assert t.gate3_pending is False, "MERGED 意味着机器自己判完了，没人在等"


def test_auto_landed_tasks_do_not_inflate_the_backlog(store):
    """8 个自动落地 + 1 个真上人 → 积压数必须是 1。

    这个数是「该往哪投人」的依据。它把常态算进去的话，读数永远接近任务总数，
    于是没人会拿它做决定 —— 一个没人看的指标和没有这个指标一样。
    """
    for i in range(8):
        store.finalize(_attempt(store, f"T-auto-{i}"), Resolution.MERGED)
    store.finalize(_attempt(store, "T-escalated"), Resolution.ESCALATED)
    led = human_time(store)
    assert led.gate3_pending_tasks == 1
    assert [t.task_id for t in led.tasks if t.gate3_pending] == ["T-escalated"]


def test_an_auto_landed_task_is_not_a_row_in_the_detail_table(store):
    """明细表答的是「人花在哪」。没花人的任务在那张表上是纯噪音。"""
    store.finalize(_attempt(store, "T-auto"), Resolution.MERGED)
    store.finalize(_attempt(store, "T-human"), Resolution.ESCALATED)
    led = human_time(store)
    assert [t.task_id for t in led.tasks_with_data] == ["T-human"]
    assert len(led.tasks) == 2, "口径层仍留全集，只是展示层不画"


def test_an_escalated_task_is_still_waiting(store):
    """反面：ESCALATED 就是「机器判不了，上人」，这个必须还算积压。

    和上面几条一起钉住判据是 resolution 本身，而不是「有没有 override 事件」。
    """
    store.finalize(_attempt(store), Resolution.ESCALATED)
    (t,) = human_time(store).tasks
    assert t.gate3_pending is True


def test_the_latest_round_decides_whether_a_human_is_waiting(store, monkeypatch):
    """打回一轮再升级：判据取最后一轮。

    中间轮次是 REWORKED（worker 自己重跑，没人在等）。拿第一轮判会说没人等，
    可这个任务此刻正躺在人的桌上。
    """
    t0 = utc_now() - timedelta(hours=1)
    stamps = iter([t0, t0 + timedelta(minutes=30)])
    monkeypatch.setattr("factory.audit.store.utc_now", lambda: next(stamps))
    store.finalize(_attempt(store, "T-1"), Resolution.REWORKED)
    store.finalize(_attempt(store, "T-1"), Resolution.ESCALATED)
    monkeypatch.undo()
    (t,) = human_time(store).tasks
    assert t.gate3_pending is True


def test_a_reworked_round_alone_is_not_waiting_on_a_human(store):
    """只被打回、还没升级：worker 会自己重跑，人没在等。"""
    store.finalize(_attempt(store), Resolution.REWORKED)
    (t,) = human_time(store).tasks
    assert t.gate3_pending is False


def test_gate3_uses_the_latest_resolved_attempt_before_the_override(
    store, monkeypatch,
):
    """一个任务打回两轮：人验收的是最后那一轮的判决，不是第一轮的。
    取第一轮会把中间 worker 重跑的两小时全算成人在盯屏幕。

    两轮的 resolved_at 必须**真的隔开**：都用 utc_now 落的话两轮只差几微秒，
    「取第一轮」和「取最后一轮」算出来的数就区分不开，这条断言等于没写。
    """
    t0 = utc_now() - timedelta(hours=3)
    stamps = iter([t0, t0 + timedelta(hours=2)])
    monkeypatch.setattr("factory.audit.store.utc_now", lambda: next(stamps))

    a1 = _attempt(store, "T-1")
    store.finalize(a1, Resolution.REWORKED)          # 第一轮判决：t0
    a2 = _attempt(store, "T-1")
    store.finalize(a2, Resolution.ESCALATED)         # 第二轮判决：t0 + 2h
    monkeypatch.undo()

    _override_event(store, "T-1", t0 + timedelta(hours=2, minutes=4))
    (t,) = human_time(store).tasks
    assert t.gate3_seconds == pytest.approx(240.0), "只算最后一轮判决之后那 4 分钟"


def test_two_overrides_on_one_ruling_are_charged_once(store):
    """人把 override 跑了两遍（手滑 / 改主意）：那一轮只许收一次费。

    每个 override 各自减一次 resolved_at 的话，两次命令 = 两倍人时。这里人最多
    花了 11 分钟，按每个都算会报 21 分钟。这个方向让系统显得更费人（不是更好看），
    但一样是错的 —— 这张表要拿去做投工决定，虚高和虚低一样没法用。
    """
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    r = store.get(aid).resolved_at
    _override_event(store, "T-1", r + 10 * MIN)
    _override_event(store, "T-1", r + 11 * MIN)

    led = human_time(store)
    (t,) = led.tasks
    assert t.gate3_seconds == pytest.approx(600.0, abs=2.0), "取最早那次定案"
    assert led.unpaired_events == 0, (
        "重复定案不是「配不上对」—— 那个计数器的意思是有人绕过命令动了队列，"
        "混进来会报假警"
    )


def test_each_round_is_charged_separately(store, monkeypatch):
    """反面：真的两轮判决、两次定案，两段都得算。

    和上一条一起钉住去重的粒度是「每轮判决一次」，不是「整个任务一次」。
    """
    t0 = utc_now() - timedelta(hours=5)
    stamps = iter([t0, t0 + timedelta(hours=2)])
    monkeypatch.setattr("factory.audit.store.utc_now", lambda: next(stamps))
    store.finalize(_attempt(store, "T-1"), Resolution.REWORKED)
    store.finalize(_attempt(store, "T-1"), Resolution.ESCALATED)
    monkeypatch.undo()

    _override_event(store, "T-1", t0 + timedelta(minutes=3))          # 第一轮 3min
    _override_event(store, "T-1", t0 + timedelta(hours=2, minutes=4))  # 第二轮 4min
    (t,) = human_time(store).tasks
    assert t.gate3_seconds == pytest.approx(420.0), "3min + 4min，两轮各收一次"


def test_a_task_that_bounced_back_to_a_human_is_waiting_again(store, monkeypatch):
    """人定过案 → worker 又跑一轮 → 又判不了。这一刻是重新在等人。

    守卫那行（定过案的就不算在等）必须只掐**那一轮**。要是拿「这个任务有没有被
    定过案」当判据，第二次上人就永远看不见了 —— 而这条路恰恰是最该被看见的：
    一个来回过两遍人手的任务，正是投工该先看的。

    另外钉住等待时长从**第二轮**判决算起，不是第一轮：从第一轮算会把中间
    worker 自己在跑的两小时算进人的等待里。
    """
    t0 = utc_now() - timedelta(hours=4)
    stamps = iter([t0, t0 + timedelta(hours=2)])
    monkeypatch.setattr("factory.audit.store.utc_now", lambda: next(stamps))
    store.finalize(_attempt(store, "T-1"), Resolution.ESCALATED)   # 第一轮
    store.finalize(_attempt(store, "T-1"), Resolution.ESCALATED)   # 第二轮，没人管
    monkeypatch.undo()

    _override_event(store, "T-1", t0 + timedelta(minutes=5))  # 只定了第一轮

    (t,) = human_time(store, now=t0 + timedelta(hours=4)).tasks
    assert t.gate3_seconds == pytest.approx(300.0), "第一轮那 5min 照收"
    assert t.gate3_pending is True, "第二轮还挂在人手上"
    assert t.waiting_seconds == pytest.approx(7200.0), (
        "从第二轮判决算起 2h；从第一轮算会把 worker 重跑那段算成人在等"
    )


def test_an_override_before_any_resolution_is_unpaired(store):
    """override 早于 resolved_at 会算出负数。负人时是记错了，不是省下来了。"""
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    _override_event(store, "T-1", store.get(aid).resolved_at - 5 * MIN)
    led = human_time(store)
    assert led.tasks[0].gate3_seconds is None
    assert led.unpaired_events == 1


# ---------- 合计、墙钟、分布 ----------

def test_human_total_is_the_sum_of_both_gates(store):
    t0 = utc_now()
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 5 * MIN)
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    _override_event(store, "T-1", store.get(aid).resolved_at + 3 * MIN)

    led = human_time(store)
    assert led.human_total == pytest.approx(480.0, abs=2.0)
    assert led.gate1_total == pytest.approx(300.0)
    assert led.gate3_total == pytest.approx(180.0, abs=2.0)
    assert led.tasks[0].human_seconds == pytest.approx(480.0, abs=2.0)


def test_wall_clock_runs_from_the_first_draft_event_to_resolution(store):
    """需求→上线的墙钟。起点是草稿第一次被拦（人第一次看到它），
    终点是最后一轮判决落下 —— 中间 worker 跑了多久、人等了多久都在里面。"""
    t0 = utc_now() - timedelta(hours=6)
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 10 * MIN)
    aid = _attempt(store)
    store.finalize(aid, Resolution.MERGED)

    (t,) = human_time(store).tasks
    assert t.wall_clock_seconds == pytest.approx(6 * 3600, abs=30.0)
    assert t.human_ratio == pytest.approx(t.human_seconds / t.wall_clock_seconds)


def test_human_time_never_exceeds_the_wall_clock(store):
    """人时占比不许超过 100%。

    人花的时间比这个需求存在的时间还长，是个当场就说不通的数。第一次真跑就
    跑出 109.8%：墙钟终点只取 resolved_at，而 override 永远晚于它，于是闸门 3
    那段人时整个落在窗口之外。终点必须同时看判决时刻和人定案的时刻。
    """
    t0 = utc_now() - timedelta(minutes=20)
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 5 * MIN)
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    _override_event(store, "T-1", store.get(aid).resolved_at + 3 * MIN)

    (t,) = human_time(store).tasks
    assert t.human_seconds <= t.wall_clock_seconds
    assert t.human_ratio <= 1.0


def test_wall_clock_is_none_until_something_resolves(store):
    _blocked(store, "T-1", utc_now())
    (t,) = human_time(store).tasks
    assert t.wall_clock_seconds is None
    assert t.human_ratio is None


def test_means_and_max_skip_tasks_with_no_measurement(store):
    """分母只取量到了的那些。把 pending 当 0 进分母会把均值往下拽。"""
    t0 = utc_now()
    _blocked(store, "T-1", t0)
    _confirm(store, "T-1", t0 + 2 * MIN)
    _blocked(store, "T-2", t0)
    _confirm(store, "T-2", t0 + 6 * MIN)
    _blocked(store, "T-3", t0)          # 还在等人

    led = human_time(store)
    assert led.gate1_mean == pytest.approx(240.0), "只按两个量到的算"
    assert led.gate1_max == pytest.approx(360.0)
    assert led.gate1_pending_tasks == 1


def test_tasks_are_ordered_by_human_time_descending(store):
    """要答的是「下一步该往哪投工」，最费人的那个得在最上面。

    task_id 刻意取成**字母序和人时序相反**：叫 T-big / T-mid / T-small 的话
    两种排法算出来一模一样，这条断言对「照 task_id 排」的实现完全免疫。
    """
    t0 = utc_now()
    for tid, mins in (("T-a", 1), ("T-z", 40), ("T-m", 9)):
        _blocked(store, tid, t0)
        _confirm(store, tid, t0 + mins * MIN)
    assert [t.task_id for t in human_time(store).tasks] == ["T-z", "T-m", "T-a"]


def test_a_single_task_can_be_scoped(store):
    t0 = utc_now()
    for tid in ("T-1", "T-2"):
        _blocked(store, tid, t0)
        _confirm(store, tid, t0 + 3 * MIN)
    led = human_time(store, task_id="T-2")
    assert [t.task_id for t in led.tasks] == ["T-2"]
    assert led.gate1_total == pytest.approx(180.0)


def test_an_unjudged_attempt_is_not_shown_as_a_row_with_data(store):
    """刚派出去还没判的 attempt 在 tasks 里留一行全 None。

    它在展示层是纯噪音，更要紧的是它会把「这库还没人时数据」的空态提示挤掉 ——
    于是一张什么都没量到的表看起来像一张量过的表。这正是这个项目反复吃亏的
    形状（空表和干净的表长得一样），所以判据分两层：tasks 保留全集，
    tasks_with_data 才是能画的那些。
    """
    _attempt(store)
    led = human_time(store)
    assert len(led.tasks) == 1, "口径层不替展示层做减法"
    assert led.tasks_with_data == ()


def test_a_task_waiting_on_a_human_counts_as_having_data(store):
    """在等人没量到用时，但那本身就是要看的东西（积压），不许被过滤掉。"""
    _blocked(store, "T-1", utc_now())
    assert len(human_time(store).tasks_with_data) == 1


# ---------- CLI：factory metrics --human ----------

def test_metrics_without_human_does_not_print_the_ledger(store, tmp_path, capsys):
    """默认不打。既有输出被硬编码进别处的断言和人的眼睛里，不该悄悄变长。"""
    import argparse

    from factory.cli import _cmd_metrics

    _blocked(store, "T-1", utc_now())
    _cmd_metrics(argparse.Namespace(db=str(tmp_path / "audit.db"),
                                    task_id=None, human=False))
    assert "[人时账]" not in capsys.readouterr().out


def test_metrics_human_prints_both_gates(store, tmp_path, capsys):
    import argparse

    from factory.cli import _cmd_metrics

    t0 = utc_now()
    _blocked(store, "T-一号需求", t0)
    _confirm(store, "T-一号需求", t0 + 5 * MIN)
    aid = _attempt(store, "T-一号需求")
    store.finalize(aid, Resolution.ESCALATED)
    _override_event(store, "T-一号需求", store.get(aid).resolved_at + 3 * MIN)

    _cmd_metrics(argparse.Namespace(db=str(tmp_path / "audit.db"),
                                    task_id=None, human=True))
    out = capsys.readouterr().out
    assert "[人时账]" in out
    assert "闸门 1 确认需求" in out and "闸门 3 验收交付" in out
    assert "5.0min" in out and "T-一号需求" in out


def test_metrics_human_prints_a_dash_not_a_zero_when_empty(store, tmp_path, capsys):
    """空库打 0min 会被读成「已经无人了」。这是这张表最容易骗人的地方。"""
    import argparse

    from factory.cli import _cmd_metrics

    _cmd_metrics(argparse.Namespace(db=str(tmp_path / "audit.db"),
                                    task_id=None, human=True))
    out = capsys.readouterr().out
    assert "还没有人时数据" in out
    assert "0min" not in out and "0s" not in out


def test_metrics_human_marks_tasks_still_waiting(store, tmp_path, capsys):
    import argparse

    from factory.cli import _cmd_metrics

    _blocked(store, "T-等着", utc_now())
    _cmd_metrics(argparse.Namespace(db=str(tmp_path / "audit.db"),
                                    task_id=None, human=True))
    out = capsys.readouterr().out
    assert "等人确认" in out
    # 那一行的闸门 1 必须是「—」而不是任何数：还在等人不是花了 0 分钟。
    (line,) = [ln for ln in out.splitlines()
               if "T-等着" in ln and "闸门1" in ln]
    assert "闸门1       —" in line, line


def test_metrics_human_prints_how_long_the_backlog_has_waited(
    store, tmp_path, capsys,
):
    """积压得说出「等了多久 / 谁在等」，不能只说「有几个」。

    只报个数的话，一个刚判完的队列和一个积了三天的队列打出来一样，那行字就
    没法拿来排优先级。
    """
    import argparse

    from factory.cli import _cmd_metrics

    aid = _attempt(store, "T-躺了三天")
    store.finalize(aid, Resolution.ESCALATED)
    # 把判决时刻推到三天前：等待时长是「此刻 − 判决时刻」，不推的话是 0 秒，
    # 「等了多久」这件事在断言里就区分不出对错。
    with store._session() as s:  # noqa: SLF001 - 造积压只能直接改时间戳
        s.get(type(store.get(aid)), aid).resolved_at = utc_now() - timedelta(days=3)
        s.commit()

    _cmd_metrics(argparse.Namespace(db=str(tmp_path / "audit.db"),
                                    task_id=None, human=True))
    out = capsys.readouterr().out
    assert "T-躺了三天" in out
    (summary,) = [ln for ln in out.splitlines() if "等最久" in ln]
    assert "T-躺了三天" in summary, summary
    assert "3.0d" in summary, summary
    assert "不算人时" in summary, "得说清这段不进人时，否则会被读成人花的时间"
    # 明细那一行也得带上等待时长，不能只在汇总里出现一次。
    (row,) = [ln for ln in out.splitlines()
              if "T-躺了三天" in ln and "闸门1" in ln]
    assert "已等 " in row and "3.0d" in row, row


def test_the_ledger_does_not_touch_gate3_rework(store):
    """人时账不许改动既有 P1 指标的口径。同一份数据两个函数各算各的。"""
    from factory.metrics import gate3_rework

    aid = _attempt(store)
    store.finalize(aid, Resolution.REWORKED)
    _blocked(store, "T-1", utc_now())
    r = gate3_rework(store)
    assert (r.tasks, r.total_reworks) == (1, 1)
    assert r.mean_reworks == pytest.approx(1.0)
