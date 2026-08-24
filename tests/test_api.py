"""只读 JSON API 的形状测试。

三个纯函数（`list_tasks` / `task_detail` / `global_stats`）不知道 HTTP 存在，
所以这里一次都不起服务器：没有端口冲突、没有 sleep 等启动、没有 flaky。
`route()` 也是纯函数（吃路径吐 `(code, dict)`），所以 404 / 500 的形状同样
能直接断言。

夹具都建在 `tmp_path` 上，不碰 `/home/ubuntu/factory-data` —— 那是真实运行
数据，测试读它会在别人跑批时随机变绿变红，而且写它就等于污染审计轨迹。

**这些测试守的是契约里那份响应形状**（`contracts/api-shape.md`）：前端
TypeScript 类型按它写死了，后端改个键名、把 `wall_clock_s` 又换回毫秒、
或者让某个桶在没数据时消失，前端就是运行时炸而不是编译期报错。所以断言
落在键的存在性和单位上，不只是「返回了 200」。
"""

from __future__ import annotations

import pytest

from factory.api import (
    BUCKETS,
    QueueUnreadable,
    global_stats,
    list_tasks,
    route,
    task_detail,
)
from factory.audit.models import NOT_DISPATCHED, OracleClass, Resolution, SupervisorRole, Verdict
from factory.audit.store import AuditStore
from factory.backlog.store import Backlog

TASK = "T-console-empty-hint"

_YAML = """\
task_id: {task_id}
prompt: |
  factory/console_read.py 的 read_threads() 现在返回 (threads, error) 两元组。
  队列可读但一个任务都没有时返回 ([], "")，调用方无法区分两种情况。
checks:
  - name: pytest_console
    command: uv run pytest tests/test_console.py -q
    expect: exit_zero
    timeout_s: 300
"""


@pytest.fixture
def queue(tmp_path):
    """一个真队列：六个子目录都在，`done/` 里放着那个跑完的任务。"""
    root = tmp_path / "queue"
    Backlog(root).ensure()
    (root / "done" / f"{TASK}.yaml").write_text(
        _YAML.format(task_id=TASK), encoding="utf-8")
    return root


@pytest.fixture
def audit_db(tmp_path):
    """一个有 9 轮 attempt 的审计库，最后一轮 merged。

    形状照抄真实数据（`/home/ubuntu/factory-data/audit.db` 里那条）：前 8 轮
    reworked/escalated，第 9 轮 merged 带 commit。刻意不用真库 —— 见模块 docstring。
    """
    path = tmp_path / "audit.db"
    store = AuditStore(path)
    for n in range(1, 10):
        merged = n == 9
        attempt_id = store.open_attempt(
            task_id=TASK, spec_ref=[], oracle_class=OracleClass.A,
            class_reason="no rule matched -> default A",
            harness="claude_code", harness_version="2.1.234",
            model="sonnet" if merged else "haiku",
        )
        store.record_result(
            attempt_id, diff_hash=None,
            commit="9f8aa24d13b82690b3c311f757b8b1b314d85993" if merged else None,
            transcript_path=None, tokens_in=5000, tokens_out=800,
            cost_usd=2.909712 if merged else 0.4724,
            wall_clock_ms=45200,
        )
        store.record_verdict(attempt_id, role=SupervisorRole.REGRESSION,
                             verdict=Verdict.PASS if merged else Verdict.FAIL,
                             claims=[] if merged else [{"check": "pytest_console"}])
        store.finalize(attempt_id,
                       Resolution.MERGED if merged else Resolution.REWORKED)
    return path


# ---------- 契约里给的四条 ----------


def test_list_tasks(tmp_path):
    """list_tasks 返回五个状态桶。"""
    for d in ("inbox", "running", "done", "needs-human", "blocked"):
        (tmp_path / d).mkdir()
    out = list_tasks(tmp_path)
    assert set(out) == {"inbox", "running", "done", "needs_human", "blocked"}


def test_task_detail_has_attempts(audit_db, queue):
    """task_detail 带出轮次列表，按 attempt_no 升序。"""
    out = task_detail(audit_db, queue, TASK)
    assert out["task_id"] == TASK
    nos = [a["attempt_no"] for a in out["attempts"]]
    assert nos == sorted(nos)
    assert len(nos) == 9


def test_task_detail_missing_returns_none(audit_db, queue):
    """不存在的 task_id 返回 None，由 HTTP 层转 404。"""
    assert task_detail(audit_db, queue, "T-nope") is None


def test_global_stats(audit_db, queue):
    """global_stats 汇总任务数与花费。"""
    out = global_stats(audit_db, queue)
    assert out["total_tasks"] >= 0
    assert out["total_cost_usd"] >= 0


# ---------- 队列读不出来必须炸，不许静默返回空 ----------


@pytest.mark.parametrize("broken", ["missing", "a-file", "empty-dir"])
def test_unreadable_queue_raises(tmp_path, broken):
    """路径写错 / 指到文件 / 不像队列的目录，都抛异常。

    这是这一层最重要的一条：读不到队列和队列是空的，在前端长得一模一样，
    而前者意味着这一页在说谎。静默返回五个空桶会把「路径写错了」渲染成
    「今天没任务」—— 一个安静的、没人会来查的错误。
    """
    if broken == "missing":
        target = tmp_path / "nope"
    elif broken == "a-file":
        target = tmp_path / "afile"
        target.write_text("not a queue", encoding="utf-8")
    else:
        target = tmp_path / "empty"
        target.mkdir()
    with pytest.raises(QueueUnreadable):
        list_tasks(target)


def test_empty_but_real_queue_is_not_an_error(tmp_path):
    """刚 ensure() 过的空队列是**真的空**，该正常返回五个空桶。

    判据是「有没有状态子目录」而不是「有没有条目」，否则一个干净的新队列
    会被判成读取失败。
    """
    root = tmp_path / "q"
    Backlog(root).ensure()
    out = list_tasks(root)
    assert out == {b: [] for b in BUCKETS.values()}


# ---------- 响应形状（前端 TS 类型按这个写死了） ----------


def test_list_tasks_summary_shape(audit_db, queue):
    """done 桶的条目带审计数字，inbox 桶的不带。"""
    out = list_tasks(queue, audit_db)
    (row,) = out["done"]
    assert row["task_id"] == TASK
    assert row["checks_count"] == 1
    assert row["mtime"]                      # ISO 串，非空
    # 跑过的桶才有这三个
    assert row["resolution"] == "merged"
    assert row["attempts_count"] == 9
    assert row["total_cost_usd"] == pytest.approx(0.4724 * 8 + 2.909712)


def test_inbox_entry_has_no_audit_fields(tmp_path, audit_db):
    """还没跑过的任务不带 total_cost_usd —— 缺字段 ≠ 花了 $0。

    塞一个 0 进去会让前端显示「花了 $0」，那和「真的免费跑完了」长得一样。
    """
    root = tmp_path / "q"
    Backlog(root).ensure()
    (root / "inbox" / "T-new.yaml").write_text(
        _YAML.format(task_id="T-new"), encoding="utf-8")
    (row,) = list_tasks(root, audit_db)["inbox"]
    assert row["task_id"] == "T-new"
    assert "total_cost_usd" not in row
    assert "resolution" not in row
    assert "attempts_count" not in row


def test_prompt_preview_is_truncated(queue, audit_db):
    """prompt_preview 是一行、最多 60 字符 + 省略号；prompt 全文在详情里。"""
    (row,) = list_tasks(queue, audit_db)["done"]
    assert "\n" not in row["prompt_preview"]
    assert len(row["prompt_preview"]) <= 61
    assert row["prompt_preview"].endswith("…")
    # 全文只在详情端点，不在列表里
    assert "\n" in task_detail(audit_db, queue, TASK)["prompt"]


def test_task_detail_shape(audit_db, queue):
    """详情的每个键都在，且 wall_clock 是**秒**不是毫秒。"""
    out = task_detail(audit_db, queue, TASK)
    assert out["state"] == "done"
    assert out["checks"] == [{
        "name": "pytest_console",
        "command": "uv run pytest tests/test_console.py -q",
    }]
    assert out["oracle_class"] == "A"
    assert out["class_reason"]
    # 9 轮 × 45200ms = 406.8s。契约要求后端做单位换算，
    # 前端除 1000 的话漏一处就差三个数量级。
    assert out["total_wall_clock_s"] == pytest.approx(406.8)

    last = out["attempts"][-1]
    assert last["attempt_no"] == 9
    assert last["resolution"] == "merged"
    assert last["commit"] == "9f8aa24d13b82690b3c311f757b8b1b314d85993"
    assert last["wall_clock_s"] == pytest.approx(45.2)
    assert last["created_at"]
    # 前几轮没 commit —— 只有合并的那轮有
    assert out["attempts"][0]["commit"] is None


def test_verdict_claims_never_null(audit_db, queue):
    """claims 恒为数组。null 会让前端 `.map()` 直接抛。"""
    out = task_detail(audit_db, queue, TASK)
    for attempt in out["attempts"]:
        for v in attempt["verdicts"]:
            assert isinstance(v["claims"], list)
            assert v["role"] == "regression"
            assert v["verdict"] in ("pass", "fail")


def test_detail_works_when_queue_entry_was_cleaned_up(audit_db, tmp_path):
    """队列条目被清了但审计轨迹还在 —— 详情页照样该打得开。"""
    root = tmp_path / "q"
    Backlog(root).ensure()
    out = task_detail(audit_db, root, TASK)
    assert out is not None
    assert out["state"] is None          # 队列里已经没有它
    assert len(out["attempts"]) == 9     # 但轨迹全在
    assert out["prompt"] == ""


def test_not_dispatched_resolution(tmp_path):
    """预分级拦下的轮次记成 not_dispatched，不是 pending。

    判据落在 harness_version 上（`NOT_DISPATCHED`），读错字段会让 D 类硬闸门
    在前端显示成「还在跑」，而那些轮次从没调过 harness、永远不会有结果。
    """
    root = tmp_path / "q"
    Backlog(root).ensure()
    db = tmp_path / "a.db"
    store = AuditStore(db)
    attempt_id = store.open_attempt(
        task_id="T-d", spec_ref=[], oracle_class=OracleClass.D,
        class_reason="不可逆操作", harness="claude_code",
        harness_version=NOT_DISPATCHED, model="opus")
    assert store.get(attempt_id) is not None
    out = task_detail(db, root, "T-d")
    assert out["attempts"][0]["resolution"] == "not_dispatched"


def test_global_stats_shape(audit_db, queue):
    """统计的每个键都在，by_state 五个桶恒定存在。"""
    out = global_stats(audit_db, queue)
    assert out["total_tasks"] == 1
    assert out["total_attempts"] == 9
    assert out["by_resolution"] == {"reworked": 8, "merged": 1}
    assert out["by_state"] == {"inbox": 0, "running": 0, "done": 1,
                               "needs_human": 0, "blocked": 0}
    assert out["avg_attempts_per_task"] == pytest.approx(9.0)
    assert out["avg_cost_per_task_usd"] == pytest.approx(out["total_cost_usd"])


def test_global_stats_on_empty_db_does_not_divide_by_zero(tmp_path):
    """一次都没跑过时不许炸（0 个任务的平均值）。"""
    root = tmp_path / "q"
    Backlog(root).ensure()
    out = global_stats(tmp_path / "empty.db", root)
    assert out["total_tasks"] == 0
    assert out["avg_cost_per_task_usd"] == 0
    assert out["avg_attempts_per_task"] == 0


def test_stats_survives_unreadable_queue(audit_db, tmp_path):
    """队列读不出来时统计仍返回审计侧的数，但 by_state 是 None 不是 0。

    五个 0 会在前端显示成「队列是空的」；None 渲染成 `—`，说的是「不知道」。
    """
    out = global_stats(audit_db, tmp_path / "nope")
    assert out["total_attempts"] == 9
    assert out["by_state"] == dict.fromkeys(BUCKETS.values(), None)


# ---------- HTTP 路由层（纯函数，不起服务器） ----------


def test_route_ok(audit_db, queue):
    """三个端点都回 200。"""
    for path in ("/api/tasks", "/api/stats", f"/api/task/{TASK}"):
        code, payload = route(path, db=audit_db, queue=queue)
        assert code == 200, (path, payload)
        assert "error" not in payload


def test_route_missing_task_is_404(audit_db, queue):
    """task_id 不存在 → 404 + {"error": ...}，契约里写死了这句话的形状。"""
    code, payload = route("/api/task/T-nope", db=audit_db, queue=queue)
    assert code == 404
    assert payload == {"error": "task not found: T-nope"}


def test_route_unknown_endpoint_is_404(audit_db, queue):
    code, payload = route("/api/nope", db=audit_db, queue=queue)
    assert code == 404
    assert "error" in payload


def test_route_unreadable_queue_is_500(audit_db, tmp_path):
    """读不到队列 → 500，不是 200 + 空数据。前端必须知道这一页在说谎。"""
    code, payload = route("/api/tasks", db=audit_db, queue=tmp_path / "nope")
    assert code == 500
    assert "error" in payload


def test_route_ignores_query_string_and_trailing_slash(audit_db, queue):
    """`/api/tasks/?t=1` 和 `/api/tasks` 是同一个端点（前端会带缓存参数）。"""
    code, _ = route("/api/tasks/?t=1755500000", db=audit_db, queue=queue)
    assert code == 200


def test_route_decodes_task_id(audit_db, queue):
    """task_id 走 URL 解码 —— 中文或空格的 id 在地址栏里是百分号编码的。"""
    code, payload = route("/api/task/T-nope%20x", db=audit_db, queue=queue)
    assert code == 404
    assert payload["error"] == "task not found: T-nope x"
