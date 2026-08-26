"""人工介入动作：定案、验收、重跑。

这些端点是这个系统里唯一会**写审计库**的 HTTP 入口，所以测试的重点不是
「happy path 能跑」，而是三件事：

1. 拒绝没有理由的介入（没理由的定案在审计里等于没发生）
2. 「审计先落、队列后搬」的次序不能反（反了会造出两条都没定案的 attempt）
3. 连点两次不产生两条记录（网络慢时人一定会连点）
"""

from __future__ import annotations

import pytest

from factory.api import route_post
from factory.api_write import do_accept, do_rerun, do_resolve
from factory.audit.models import Resolution
from factory.audit.store import AuditStore
from factory.backlog.store import Backlog, INBOX, NEEDS_HUMAN


@pytest.fixture()
def env(tmp_path):
    """一个卡在 needs-human 的任务 + 它在审计里的那一轮。"""
    queue = tmp_path / "queue"
    b = Backlog(queue)
    b.ensure()
    parked = b.dir(NEEDS_HUMAN) / "T-demo.1.yaml"
    parked.write_text("id: T-demo\ngoal: 干点什么\n", encoding="utf-8")
    # revive() 要求 .result.json 在（不然 CLI 会让人加 --force）
    (parked.with_suffix(".yaml.result.json")).write_text("{}", encoding="utf-8")

    db = tmp_path / "audit.db"
    store = AuditStore(db)
    aid = store.open_attempt(
        task_id="T-demo", spec_ref=[], oracle_class="A", class_reason="t",
        harness="claude_code", harness_version="1.0", model="opus",
    )
    return {"queue": queue, "db": db, "store": store, "attempt_id": aid,
            "parked": parked}


# ---------- 定案 ----------


def test_定案落审计且带理由(env):
    got = do_resolve("T-demo", {"resolution": "merged", "note": "人核过，判据写窄了"},
                     db=env["db"], queue=env["queue"])

    assert got.code == 200
    row = AuditStore(env["db"]).latest_attempt("T-demo")
    assert row is not None
    assert row.resolution == Resolution.MERGED
    assert row.resolution_note == "人核过，判据写窄了"


def test_定案不动队列(env):
    """定案 ≠ 重跑。任务必须留在 needs-human/。"""
    do_resolve("T-demo", {"resolution": "merged", "note": "ok"},
               db=env["db"], queue=env["queue"])

    assert env["parked"].exists()
    assert not list(Backlog(env["queue"]).dir(INBOX).glob("T-demo*"))


def test_空理由被拒(env):
    got = do_resolve("T-demo", {"resolution": "merged", "note": "   "},
                     db=env["db"], queue=env["queue"])

    assert got.code == 400
    assert "必须写定案理由" in got.payload["error"]
    # 关键：被拒的请求不能留下半个定案
    assert AuditStore(env["db"]).latest_attempt("T-demo").resolution == Resolution.PENDING


def test_只认merged和reworked(env):
    """pending / escalated 是系统写的中间态，让人手填会污染 metrics。"""
    for word in ("pending", "escalated", "human_override", ""):
        got = do_resolve("T-demo", {"resolution": word, "note": "x"},
                         db=env["db"], queue=env["queue"])
        assert got.code == 400, f"{word!r} 不该被接受"
        assert "allowed" in got.payload


def test_超长理由被拒而不是静默截断(env):
    """库里那列是 VARCHAR(512)。默默截掉人打的 300 字比报错糟。"""
    got = do_resolve("T-demo", {"resolution": "merged", "note": "长" * 600},
                     db=env["db"], queue=env["queue"])

    assert got.code == 400
    assert "512" in got.payload["error"]


def test_没派发过的任务定案报409(env):
    """死锁 park、手动扔进来的任务从没跑过 —— 不是 500，是状态冲突。"""
    got = do_resolve("T-never-ran", {"resolution": "merged", "note": "x"},
                     db=env["db"], queue=env["queue"])

    assert got.code == 409
    assert "没派发过" in got.payload["error"]


# ---------- 重跑：审计先落，队列后搬 ----------


def test_重跑既定案又放回inbox(env):
    got = do_rerun("T-demo", {"note": "改了 checks，这轮的红是真的"},
                   db=env["db"], queue=env["queue"])

    assert got.code == 200, got.payload
    row = AuditStore(env["db"]).latest_attempt("T-demo")
    assert row.resolution == Resolution.REWORKED
    assert row.resolution_note == "改了 checks，这轮的红是真的"
    # 队列侧：从 needs-human 搬到 inbox
    assert not env["parked"].exists()
    assert (Backlog(env["queue"]).dir(INBOX) / "T-demo.1.yaml").exists()


def test_重跑固定reworked不接受merged(env):
    """允许 merged + requeue 会造出「已合并但又在跑」，metrics 没法解释。

    所以 do_rerun 根本不读 resolution 字段 —— 前端传什么都当 reworked。
    """
    do_rerun("T-demo", {"note": "x", "resolution": "merged"},
             db=env["db"], queue=env["queue"])

    assert AuditStore(env["db"]).latest_attempt("T-demo").resolution == Resolution.REWORKED


def test_重跑连点两次第二次给可懂的话(env):
    """网络慢时人一定连点。第二次不能是 500 traceback。"""
    first = do_rerun("T-demo", {"note": "第一次"}, db=env["db"], queue=env["queue"])
    assert first.code == 200

    second = do_rerun("T-demo", {"note": "第二次"}, db=env["db"], queue=env["queue"])
    assert second.code == 409
    # 已经不在 needs-human 了 —— 要说清是「没有可放回的条目」
    assert "不在 needs-human" in second.payload["error"]
    assert "hint" in second.payload


def test_重跑不在队列里的任务报409(env):
    got = do_rerun("T-nowhere", {"note": "x"}, db=env["db"], queue=env["queue"])

    assert got.code == 409
    assert "没有可放回的条目" in got.payload["error"]
    # 要解释 running/ 为什么不能硬搬，否则人会想办法绕
    assert "running" in got.payload["hint"]


def test_重跑空理由被拒且不动队列(env):
    """校验必须在任何副作用之前。"""
    got = do_rerun("T-demo", {"note": ""}, db=env["db"], queue=env["queue"])

    assert got.code == 400
    assert env["parked"].exists()  # 队列没动
    assert AuditStore(env["db"]).latest_attempt("T-demo").resolution == Resolution.PENDING
