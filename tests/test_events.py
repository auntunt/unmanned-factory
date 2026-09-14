"""旧 CLI 回放的事件形状、时间顺序与 JSONL 数据。"""

from __future__ import annotations

import json

import pytest

from factory.audit.models import Resolution, SupervisorRole, Verdict
from factory.audit.store import AuditStore
from factory.backlog.store import Backlog, INBOX, RUNNING
from factory.events import (
    Snapshot,
    replay_stream,
    replay_timeline,
    transcript_lines,
)


def _rec(kind: str, content, ts: str = "2026-08-30T10:00:00Z") -> str:
    return json.dumps({"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}})


@pytest.fixture()
def transcript(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        _rec("assistant", [{"type": "text", "text": "先看分页逻辑。"}], "2026-08-30T10:00:01Z"),
        _rec("assistant", [{"type": "tool_use", "name": "Read", "id": "t1",
                            "input": {"file_path": "api/pagination.py"}}], "2026-08-30T10:00:02Z"),
        _rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "..."}]),
        _rec("assistant", [{"type": "tool_use", "name": "Bash", "id": "t2",
                            "input": {"command": "pytest -q " + "x" * 200}}], "2026-08-30T10:00:03Z"),
        _rec("user", [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "boom"}]),
        "{not json",
        "",
    ]), encoding="utf-8")
    return p


def test_transcript_人话化且截短(transcript):
    lines = transcript_lines(transcript)
    texts = [ln.text for ln in lines]
    assert texts[0] == "先看分页逻辑。"
    assert texts[1] == "读 api/pagination.py"
    assert texts[2].startswith("跑 pytest -q") and texts[2].endswith("…")
    assert len(texts[2]) <= 96
    assert lines[3].kind == "error"
    # 坏行和空行不炸，只是被跳过
    assert len(lines) == 4
    assert lines[1].ts is not None


def test_transcript_start_偏移只读新增(transcript):
    assert len(transcript_lines(transcript, start=4)) == 1  # 只剩 t2 的 error


@pytest.fixture()
def env(tmp_path, transcript):
    queue = tmp_path / "q"
    Backlog(queue).ensure()
    (queue / INBOX / "T-1.yaml").write_text("task_id: T-1\nprompt: 修分页\n", encoding="utf-8")
    db = tmp_path / "audit.db"
    store = AuditStore(db)
    return {"queue": queue, "db": db, "store": store, "transcript": transcript}


def _open(store, task_id="T-1"):
    return store.open_attempt(
        task_id=task_id, spec_ref=[], oracle_class="B", class_reason="影响面大",
        harness="claude_code", harness_version="1", model="opus",
    )








def test_replay时间线按t排且闸门错开(env):
    store, db = env["store"], env["db"]
    aid = _open(store)
    store.record_result(aid, diff_hash="abc", commit="deadbeef", transcript_path=str(env["transcript"]),
                        tokens_in=1, tokens_out=1, cost_usd=0.5, wall_clock_ms=60_000)
    store.record_verdict(aid, role=SupervisorRole.REGRESSION, verdict=Verdict.PASS, claims=[])
    store.record_verdict(aid, role=SupervisorRole.SCOPE, verdict=Verdict.PASS, claims=[])
    store.finalize(aid, Resolution.MERGED)

    tl = replay_timeline(db, "T-1")
    types = [e["type"] for e in tl]
    assert types[0] == "task.state" and types[1] == "attempt.open"
    assert types[-1] == "task.state" and tl[-1]["state"] == "done"
    ts = [e["t"] for e in tl]
    assert ts == sorted(ts)
    # transcript 的时间戳早于 attempt.created_at（这里是过去的固定时刻）→ 均匀铺在墙钟内
    lines = [e for e in tl if e["type"] == "attempt.line"]
    assert len(lines) == 4 and all(0 < e["t"] <= 60 for e in lines)
    gates = [e for e in tl if e["type"] == "gate.verdict"]
    assert gates[1]["t"] - gates[0]["t"] == pytest.approx(1.5)
    final = next(e for e in tl if e["type"] == "attempt.final")
    assert final["commit"] == "deadbeef" and final["t"] > gates[-1]["t"]


def test_replay_stream按倍速sleep(env):
    store, db = env["store"], env["db"]
    aid = _open(store)
    store.record_result(aid, diff_hash=None, commit=None, transcript_path=None,
                        tokens_in=0, tokens_out=0, cost_usd=0.1, wall_clock_ms=8_000)
    store.finalize(aid, Resolution.MERGED)
    slept: list[float] = []
    evs = list(replay_stream(db, "T-1", speed=4, sleep=slept.append))
    assert evs[0]["type"] == "snapshot" and evs[0]["replay"] == "T-1"
    assert evs[-1]["type"] == "replay.end"
    assert sum(slept) == pytest.approx((8.0 + 1.5 + 0.5) / 4)


def test_replay没有记录只有snapshot和end(env):
    evs = list(replay_stream(env["db"], "T-nope", speed=0))
    assert [e["type"] for e in evs] == ["snapshot", "replay.end"]


def test_snapshot_public形状(env):
    snap = Snapshot(at="2026-08-31T00:00:00Z", tasks={"T-1": "inbox", "T-2": "done"})
    pub = snap.public()
    assert pub["type"] == "snapshot" and pub["tasks"] == {"inbox": ["T-1"], "done": ["T-2"]}
    assert pub["attempts"] == []
