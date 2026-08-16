"""看板上的人时账那一段。

和 test_dashboard.py 同一条规矩：测的不是「数算得对」（那在
test_human_time_metrics.py 里），是「它有没有把一个不成立的世界渲染成一张
看着没问题的页面」。这一段最容易犯的错是把「还没数据」画成 0 分钟 —— 那会
被读成「已经无人了」，而这一页的数字是拿去做决定的。
"""

from __future__ import annotations

import re
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
from factory.dashboard import collect, queue_state, render

MIN = timedelta(minutes=1)
HEAD = "<h2>人时账 · 人到底花了多久</h2>"


def _store(tmp_path) -> tuple[AuditStore, str]:
    db = str(tmp_path / "a.db")
    return AuditStore(db), db


def _attempt(store, task_id="T-1") -> int:
    return store.open_attempt(
        task_id=task_id, spec_ref=["§1"], oracle_class=OracleClass.A,
        class_reason="有可执行判据", harness="claude-code",
        harness_version="1.0", model="claude-opus-5",
    )


def _gate1(store, task_id, minutes, *, t0=None):
    t0 = t0 or utc_now()
    for action, at in (
        (HumanAction.BLOCKED, t0),
        (HumanAction.CONFIRM, t0 + minutes * MIN),
    ):
        store.record_human_event(task_id=task_id, gate=HumanGate.INTAKE,
                                 action=action, created_at=at)


def _section(page: str) -> str:
    assert HEAD in page, "人时账那一栏必须有自己的标题"
    return page.split(HEAD)[1].split("<h2>")[0]


def test_an_empty_store_says_no_data_instead_of_zero(tmp_path):
    """没数据必须写着「还没有」，不许画成 0 分钟。

    0min 会被读成「人一分钟没花」，也就是「已经无人了」—— 这一页上最贵的
    一个误读，因为客户会拿它做决定。判据落在「有没有出现一个 0 值」，不是
    落在有没有那句话：一张同时写着提示和 0min 的页面照样骗人。
    """
    store, db = _store(tmp_path)
    _attempt(store)
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    assert "还没有人时数据" in sec
    assert not re.search(r'class="n">\s*0(\.\d+)?\s*(min|h|s)', sec), sec


def test_measured_human_time_is_labelled_measured_not_assumed(tmp_path):
    """人时是**实测**的，和「假设 · 人工单任务估时」不是一类东西。

    两个数字都以小时计、都出现在人力账附近，混在一起的话，一个我们拍的数
    会借着旁边实测数的可信度被当成测量结果 —— 这一页唯一能造成真实损害的
    失效方式。所以每张卡片自己得标明身份。
    """
    store, db = _store(tmp_path)
    _gate1(store, "T-1", 6)
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    labels = re.findall(r'<div class="l">([^<]*)</div>', sec)
    assert labels, sec
    assert all("实测" in l for l in labels), labels
    assert not any("假设" in l for l in labels), labels


def test_the_assumption_cards_are_untouched(tmp_path):
    """人时账不许改动原来那六张卡片的构成（4 实测 + 2 假设）。

    既有那条断言按数量钉死了它。这里再钉一次「人时账加进来之后它还成立」——
    在别的段落里加卡片是最容易把那个不变量撞掉的改法。
    """
    store, db = _store(tmp_path)
    _gate1(store, "T-1", 6)
    page = render(collect(db), db=db, qs=queue_state(None))
    ledger = page.split("<h2>成本与人力账</h2>")[1].split("<h2>")[0]
    labels = re.findall(r'<div class="l">([^<]*)</div>', ledger)
    assert len(labels) == 6, labels
    assert sum("假设" in l for l in labels) == 2, labels


def test_both_gates_show_up_separately(tmp_path):
    """两道闸门分开显示。合成一个「人时合计」就答不了「下一步该往哪投工」。"""
    store, db = _store(tmp_path)
    t0 = utc_now()
    _gate1(store, "T-1", 5, t0=t0)
    aid = _attempt(store)
    store.finalize(aid, Resolution.ESCALATED)
    store.record_human_event(
        task_id="T-1", gate=HumanGate.DELIVERY, action=HumanAction.OVERRIDE,
        created_at=store.get(aid).resolved_at + 3 * MIN,
    )
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    assert "闸门 1" in sec and "闸门 3" in sec
    assert "5.0min" in sec, sec


def test_a_task_waiting_on_a_human_is_shown_as_waiting(tmp_path):
    """在等人的任务要看得见 —— 那是积压，不是「花了 0 分钟」。"""
    store, db = _store(tmp_path)
    store.record_human_event(task_id="T-等着", gate=HumanGate.INTAKE,
                             action=HumanAction.BLOCKED)
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    assert "等人" in sec
    (row,) = re.findall(r"<tr><td>T-等着</td>.*?</tr>", sec)
    # 判据落在**格子里没有 0**，不是「这一行里有个「—」」：那一行末尾的
    # 人时占比本来就是「—」，会把「用时格全画成 0min」的实现也放过去。
    assert "0min" not in row and "0s" not in row and "0.0" not in row, row
    assert row.count("<td>—</td>") >= 3, row


def test_an_auto_landed_task_is_not_shown_as_waiting(tmp_path):
    """全绿自动落地 = 没人在等。它是这套系统的常态，画成积压的话跑得越顺
    积压越大，那句话就没人看了。"""
    store, db = _store(tmp_path)
    store.finalize(_attempt(store, "T-auto"), Resolution.MERGED)
    page = render(collect(db), db=db, qs=queue_state(None))
    sec = _section(page)
    assert "还没有人时数据" in sec, "一个没人参与的任务不该让这一栏看起来有数据"
    assert "T-auto" not in sec, sec


def test_the_backlog_shows_how_long_it_has_waited(tmp_path):
    """积压要说「等了多久 / 谁在等」。只报个数排不了优先级。"""
    store, db = _store(tmp_path)
    aid = _attempt(store, "T-躺着")
    store.finalize(aid, Resolution.ESCALATED)
    with store._session() as s:  # noqa: SLF001 - 造积压只能直接改时间戳
        s.get(type(store.get(aid)), aid).resolved_at = utc_now() - timedelta(days=3)
        s.commit()
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    assert "T-躺着" in sec
    assert "3.0d" in sec, sec
    # 判据必须落在**说积压那句话**上。整段里另有一句固定文案「中间机器在跑的
    # 时间不算人时」，拿 `"不算人时" in sec` 当断言的话，那句话会替这条断言
    # 兜住，于是「积压旁边根本没写清这段不进人时」照样能过。
    (line,) = [s for s in sec.split("。") if "等最久" in s]
    assert "不算人时" in line, line


def test_the_wait_is_not_rendered_as_human_time(tmp_path):
    """等待时长不许进人时合计那张卡片。

    混进去的话「人时占比」会随积压涨过 100%，而那个数正是用来证明「无人」的。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store, "T-躺着")
    store.finalize(aid, Resolution.ESCALATED)
    with store._session() as s:  # noqa: SLF001
        s.get(type(store.get(aid)), aid).resolved_at = utc_now() - timedelta(days=3)
        s.commit()
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    (card,) = re.findall(r'<div class="n">([^<]*)</div>\s*'
                         r'<div class="l">实测 · 人时合计</div>', sec)
    assert card.strip() == "—", f"人一分钟没花，这里必须是「—」而不是 3d：{card}"


def test_the_per_task_table_is_ordered_by_human_time(tmp_path):
    """最费人的在最上面。这一栏要答的就是「下一步该往哪投工」。

    名字取成字母序和人时序相反 —— 否则「照 task_id 排」的实现照样绿。
    """
    store, db = _store(tmp_path)
    t0 = utc_now()
    for tid, mins in (("T-a", 1), ("T-z", 40), ("T-m", 9)):
        _gate1(store, tid, mins, t0=t0)
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    order = re.findall(r"T-[azm]", sec)
    assert order[:3] == ["T-z", "T-m", "T-a"], order


def test_task_ids_are_escaped(tmp_path):
    """task_id 来自队列文件名，人能写任何东西进去。"""
    store, db = _store(tmp_path)
    _gate1(store, "T-<script>x</script>", 3)
    sec = _section(render(collect(db), db=db, qs=queue_state(None)))
    assert "<script>x</script>" not in sec
    assert "&lt;script&gt;" in sec


def test_the_human_ledger_is_scoped_by_task_id(tmp_path):
    """`collect(task_id=...)` 收窄时人时账也得跟着收窄，
    否则单任务页上会显示全库的人时 —— 一个看着正常的错数。"""
    store, db = _store(tmp_path)
    t0 = utc_now()
    _gate1(store, "T-1", 5, t0=t0)
    _gate1(store, "T-2", 40, t0=t0)
    sec = _section(render(collect(db, task_id="T-1"), db=db,
                          qs=queue_state(None)))
    assert "T-1" in sec
    assert "T-2" not in sec, sec


def test_state_payload_does_not_leak_human_notes(tmp_path):
    """`/state.json` 里不许出现人写的备注。

    那是人手打的自由文本（「我用 xxx 手动验过了」），和 claims 的 got 同一条
    边界：整页 HTML 里有它是因为人要主动打开看，一个 3 秒被拉一次的端点不该
    背同样的东西。
    """
    from factory.dashboard import state_payload

    store, db = _store(tmp_path)
    store.record_human_event(
        task_id="T-1", gate=HumanGate.INTAKE, action=HumanAction.CONFIRM,
        note="我把接口名改了一下顺手确认的",
    )
    payload = state_payload(collect(db), queue_state(None))
    assert "顺手确认" not in repr(payload)
