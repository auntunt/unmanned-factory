"""端到端人时账：三个写入点 + 口径。

P1 判据「闸门 3 上人平均打回次数 ≤ 1」量的是验收质量，答不了「这个需求让人
花了多少分钟」。没有那个数就没法证明这套东西省了时间，也没法判断下一步该往
哪投工。这个文件里的每条断言都在钉一个「记错了会看不出来」的点。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from factory.audit.models import (
    HumanAction,
    HumanGate,
    OracleClass,
    Resolution,
)
from factory.audit.store import AuditStore
from factory.backlog.store import NEEDS_HUMAN, Backlog
from factory.cli import _admit_to_queue, _cmd_override, _cmd_queue
from factory.intake.extract import DraftTask


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch):
    """「只抱怨一次」是模块级状态，测试之间必须清掉。"""
    monkeypatch.setattr("factory.cli._HUMAN_TIME_WARNED", False)


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _blocked_draft(task_id="T-b") -> DraftTask:
    # checks 为空 → 闸门的第一条硬拦截，必进 needs-human。
    return DraftTask(task_id=task_id, prompt="改点东西", checks=())


# ---------- 写入点一：闸门 1 把草稿拦下 = 开始等人 ----------

def test_a_blocked_draft_records_the_start_of_human_time(tmp_path):
    db = tmp_path / "audit.db"
    rc = _admit_to_queue(_blocked_draft(), _ns(queue=str(tmp_path), db=str(db)))
    assert rc == 3

    (ev,) = AuditStore(db).human_events()
    assert ev.task_id == "T-b"
    assert ev.gate == HumanGate.INTAKE
    assert ev.action == HumanAction.BLOCKED


def test_an_admitted_draft_records_nothing(tmp_path):
    """闸门放行的草稿没叫人 —— 记一条会让「无人跑通」也算进人时。"""
    db = tmp_path / "audit.db"
    draft = DraftTask(task_id="T-ok", prompt="改点东西",
                      acceptance=("能跑",), declared_paths=("a.py",),
                      checks=({"name": "n", "command": "true"},))
    assert _admit_to_queue(draft, _ns(queue=str(tmp_path), db=str(db))) == 0
    assert AuditStore(db).human_events() == ()


def test_the_blocked_event_carries_which_task_it_was(tmp_path):
    """task_id 是人时账唯一贯通的键 —— 记错了整条链就对不上。"""
    db = tmp_path / "audit.db"
    for tid in ("T-1", "T-2"):
        _admit_to_queue(_blocked_draft(tid), _ns(queue=str(tmp_path), db=str(db)))
    assert [e.task_id for e in AuditStore(db).human_events()] == ["T-1", "T-2"]


# ---------- 写入点二：人把 needs-human 的草稿放回 inbox = 人做完了 ----------

def test_requeueing_from_needs_human_records_the_confirm(tmp_path):
    db = tmp_path / "audit.db"
    bl = Backlog(tmp_path).ensure()
    parked = bl.dir(NEEDS_HUMAN) / "T-p.yaml"
    parked.write_text("task_id: T-p\nprompt: x\n", encoding="utf-8")

    rc = _cmd_queue(_ns(queue=str(tmp_path), task=[str(parked)], history=0,
                        db=str(db)))
    assert rc == 0
    (ev,) = AuditStore(db).human_events()
    assert (ev.task_id, ev.gate, ev.action) == (
        "T-p", HumanGate.INTAKE, HumanAction.CONFIRM,
    )


def test_a_normal_enqueue_records_nothing(tmp_path):
    """从别处入队的任务没经过闸门 1，也就没有「人在等」这一段。"""
    db = tmp_path / "audit.db"
    src = tmp_path / "hand-written.yaml"
    src.write_text("task_id: T-h\nprompt: x\n", encoding="utf-8")
    Backlog(tmp_path).ensure()

    assert _cmd_queue(_ns(queue=str(tmp_path), task=[str(src)], history=0,
                          db=str(db))) == 0
    assert AuditStore(db).human_events() == ()


# ---------- 写入点三：override = 人验收完了 ----------

def _attempt(store, task_id="T-o") -> int:
    return store.open_attempt(
        task_id=task_id, spec_ref=[], oracle_class=OracleClass.C,
        class_reason="C: 没有廉价裁判", harness="claude_code",
        harness_version="2.1.223", model="haiku",
    )


def test_override_records_the_delivery_gate_with_the_right_task_id(tmp_path):
    """override 命令行上只有 attempt_id，人时账要的是 task_id —— 得去库里查。"""
    db = tmp_path / "audit.db"
    store = AuditStore(db)
    aid = _attempt(store, "T-真的任务")
    store.finalize(aid, Resolution.ESCALATED)

    rc = _cmd_override(_ns(db=str(db), attempt_id=aid,
                           resolution=Resolution.HUMAN_OVERRIDE.value))
    assert rc == 0
    (ev,) = store.human_events()
    assert ev.task_id == "T-真的任务"
    assert (ev.gate, ev.action) == (HumanGate.DELIVERY, HumanAction.OVERRIDE)


def test_override_on_a_missing_attempt_records_nothing(tmp_path):
    db = tmp_path / "audit.db"
    store = AuditStore(db)
    assert _cmd_override(_ns(db=str(db), attempt_id=999,
                             resolution=Resolution.MERGED.value)) == 1
    assert store.human_events() == ()


# ---------- 没给 --db：跳过 + 只抱怨一次，绝不阻断任务 ----------

def test_a_blocked_draft_without_db_still_lands_in_needs_human(tmp_path, capsys):
    """观测性写不进去不许让任务失败（和 Journal.event 同一条规矩）。"""
    rc = _admit_to_queue(_blocked_draft(), _ns(queue=str(tmp_path), db=None))
    assert rc == 3, "退出码必须还是 3 —— 拦下的语义不因为没连库而变"
    assert (Backlog(tmp_path).dir(NEEDS_HUMAN) / "T-b.yaml").is_file()
    assert "人时" in capsys.readouterr().err


def test_the_missing_db_complaint_is_printed_only_once(tmp_path, capsys):
    """一次 prd --split 会连着落好几张草稿。每张都抱怨一遍等于把真提示刷掉。"""
    for tid in ("T-1", "T-2", "T-3"):
        _admit_to_queue(_blocked_draft(tid), _ns(queue=str(tmp_path), db=None))
    err = capsys.readouterr().err
    assert err.count("[人时账]") == 1


def test_requeueing_without_db_still_enqueues(tmp_path, capsys):
    bl = Backlog(tmp_path).ensure()
    parked = bl.dir(NEEDS_HUMAN) / "T-p.yaml"
    parked.write_text("task_id: T-p\nprompt: x\n", encoding="utf-8")

    assert _cmd_queue(_ns(queue=str(tmp_path), task=[str(parked)], history=0,
                          db=None)) == 0
    assert (bl.dir("inbox") / "T-p.yaml").is_file()
    assert "[人时账]" in capsys.readouterr().err


def test_an_unwritable_db_does_not_block_the_task(tmp_path, capsys):
    """库写不进去（锁着、路径没了）也只是抱怨。fail-open 是刻意的：
    人时账是观测，拦下任务的代价比丢一条记录大得多。"""
    rc = _admit_to_queue(
        _blocked_draft(), _ns(queue=str(tmp_path), db=str(tmp_path / "no" / "x.db")),
    )
    assert rc == 3
    assert "[人时账]" in capsys.readouterr().err


def test_show_prints_resolved_at(tmp_path, capsys):
    """闸门 3 人时的起点要在审计轨迹里看得见 —— 人时账那个数得能对得上。"""
    from factory.cli import _cmd_show

    db = tmp_path / "audit.db"
    store = AuditStore(db)
    aid = _attempt(store, "T-s")
    store.finalize(aid, Resolution.ESCALATED)
    _cmd_show(_ns(db=str(db), task_id="T-s"))
    out = capsys.readouterr().out
    assert "resolved_at" in out
    assert str(store.get(aid).resolved_at) in out


def test_show_marks_an_unjudged_attempt_as_dash(tmp_path, capsys):
    """还没判的打「-」，不打一个时刻 —— 那会让人以为它已经判完在等验收。"""
    from factory.cli import _cmd_show

    db = tmp_path / "audit.db"
    _attempt(AuditStore(db), "T-s")
    _cmd_show(_ns(db=str(db), task_id="T-s"))
    assert "resolved_at: -" in capsys.readouterr().out


def test_the_printed_requeue_command_carries_the_db(tmp_path, capsys):
    """被拦的人直接复制粘贴那条命令。少了 --db，闸门 1 只有起点没有终点，
    那段用时永远算不出来 —— 而报表上看着只是「还在等人」，看不出是丢了。"""
    db = tmp_path / "audit.db"
    _admit_to_queue(_blocked_draft(), _ns(queue=str(tmp_path), db=str(db)))
    hint = [ln for ln in capsys.readouterr().out.splitlines()
            if "补齐后入队" in ln]
    assert hint and f"--db {db}" in hint[0], hint


def test_the_requeue_hint_omits_db_when_there_is_none(tmp_path, capsys):
    """没给 --db 时不许凭空印一个 `--db None` 出来让人粘贴。"""
    _admit_to_queue(_blocked_draft(), _ns(queue=str(tmp_path), db=None))
    (hint,) = [ln for ln in capsys.readouterr().out.splitlines()
               if "补齐后入队" in ln]
    assert "--db" not in hint, hint


def test_the_old_journal_events_are_still_written(tmp_path):
    """人时账不许取代 Journal。两份数据源各有各的用途（误拒率读 Journal）。"""
    from factory.backlog.journal import Journal
    from factory.backlog.store import LOG

    db = tmp_path / "audit.db"
    _admit_to_queue(_blocked_draft(), _ns(queue=str(tmp_path), db=str(db)))
    ev = Journal(Backlog(tmp_path).dir(LOG)).tail(limit=50, kind="gate")
    assert len(ev) == 1 and ev[0]["admitted"] is False
