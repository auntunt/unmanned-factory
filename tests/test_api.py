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
    route_post,
    screen_yaml,
    submit_task,
    supervisor_stats,
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


# ---------- F1 投递闸门 ----------
#
# 这些测试守的是「坏任务不入队」这条判据。它比响应形状更重要一层：形状错了前端
# 炸，闸门漏了是凌晨三点的队列熔断（坏任务在认领时才炸，且计入 unpriced streak，
# 连续两条足够停掉一整夜）。
#
# 每个用例都断言 **inbox 无残留** 而不只是状态码：返回 400 但已经写了盘，是这
# 类缺陷最可能的形态 —— 校验加在了写盘之后。

GOOD_SUBMIT = """\
task_id: T-good-one
prompt: 把 foo() 的返回值改成 tuple
acceptance:
  - foo() 返回 (a, b) 而不是 list
checks:
  - name: unit
    command: pytest tests/test_foo.py
"""

#: 六类坏 YAML。键是标签（断言失败时能一眼看出是哪类漏了），
#: 值是 (task_id, YAML 正文)。
BAD_SUBMITS = {
    "syntax": ("T-bad-syntax", "task_id: T-bad-syntax\nprompt: [unclosed\n"),
    "dangling-spec-ref": (
        "T-bad-dangling",
        "task_id: T-bad-dangling\nprompt: 改点东西\nspec_ref:\n  - AC-1\n",
    ),
    "check-missing-command": (
        "T-bad-nocmd",
        "task_id: T-bad-nocmd\nprompt: 改点东西\nacceptance:\n  - 能跑\n"
        "checks:\n  - name: unit\n",
    ),
    "missing-prompt": (
        "T-bad-noprompt",
        "task_id: T-bad-noprompt\nacceptance:\n  - 能跑\n",
    ),
    "no-acceptance": ("T-bad-noacc", "task_id: T-bad-noacc\nprompt: 随便改改\n"),
    "top-level-not-mapping": (
        "T-bad-list",
        "- task_id: T-bad-list\n- prompt: x\n",
    ),
}


@pytest.fixture
def empty_queue(tmp_path):
    """一个空的真队列。投递测试要的是「inbox 本来没东西」这个前提。"""
    root = tmp_path / "q"
    Backlog(root).ensure()
    return root


@pytest.mark.parametrize("label", sorted(BAD_SUBMITS))
def test_bad_yaml_is_rejected_and_leaves_no_trace(empty_queue, label):
    task_id, body = BAD_SUBMITS[label]
    code, payload = submit_task({"task_id": task_id, "yaml": body}, empty_queue)

    assert code == 400, f"{label} 应该被拦，实际 {code}：{payload}"
    # 判据的另一半：不许留残骸。校验写在写盘之后的话这里会红。
    assert list((empty_queue / "inbox").iterdir()) == [], \
        f"{label} 被拒了但 inbox 里有残留"


@pytest.mark.parametrize("label", sorted(BAD_SUBMITS))
def test_rejection_body_is_three_part(empty_queue, label):
    """`{error, why, how}` 三段式。前端把 why 逐条列出来、how 贴在表单下面。

    单独一个测试而不是并进上面：状态码对但 body 少一段，前端拿到 undefined
    渲染成空白 —— 那是「投递失败了但没说为什么」，比 500 更难排。
    """
    task_id, body = BAD_SUBMITS[label]
    _, payload = submit_task({"task_id": task_id, "yaml": body}, empty_queue)

    assert set(payload) >= {"error", "why", "how"}, f"{label} 的 body 不是三段式"
    assert payload["why"], f"{label} 的 why 是空的，等于没说原因"
    assert isinstance(payload["why"], list)   # JSON 数组，不是元组也不是字符串
    assert payload["how"].strip()


def test_good_yaml_still_gets_in(empty_queue):
    """闸门收紧之后合法投递照常。没有这条，「全拦住」也算判据通过。"""
    code, payload = submit_task(
        {"task_id": "T-good-one", "yaml": GOOD_SUBMIT}, empty_queue)

    assert code == 200, payload
    assert payload["ok"] is True
    assert (empty_queue / "inbox" / "T-good-one.yaml").is_file()


def test_duplicate_task_id_is_409_not_overwritten(empty_queue):
    submit_task({"task_id": "T-good-one", "yaml": GOOD_SUBMIT}, empty_queue)
    code, payload = submit_task(
        {"task_id": "T-good-one", "yaml": GOOD_SUBMIT}, empty_queue)

    assert code == 409
    assert set(payload) >= {"error", "why", "how"}


def test_submit_yaml_goes_through_safe_load(empty_queue):
    """`!!python/object` 这类标签必须在解析阶段就死掉。

    请求体来自网络。用 `yaml.load` 的话这一行能实例化任意类 —— 而原来的
    实现根本不解析，原始文本直落 inbox，然后由认领方去 load。
    """
    body = (
        "task_id: T-bad-tag\n"
        "prompt: !!python/object/apply:os.system ['echo pwned']\n"
        "acceptance:\n  - x\n"
    )
    code, _ = submit_task({"task_id": "T-bad-tag", "yaml": body}, empty_queue)

    assert code == 400
    assert list((empty_queue / "inbox").iterdir()) == []


# ---------- F2 契约文档 + 投递流水 ----------


def test_module_docstring_does_not_claim_read_only():
    """模块 docstring 不许再自称「只读」。

    这是个元测试，守的是一类特定的腐烂：`do_POST` 落地时没人回头改文档，
    于是文件开头写着「一个字节都不往数据源里写」，而底下有一条无校验的
    写路径。一份说自己只读的文档比没有文档坏 —— 它让读者跳过「这里能不能
    写」这个问题。

    断言的不是措辞，是「声称只读」和「实际有 do_POST」不能同时成立。
    """
    import factory.api as mod

    doc = mod.__doc__ or ""
    assert "POST /api/submit" in doc, "有写路径就必须在 docstring 里点名"
    # 「只读 JSON API」这类整体性声称。允许出现「audit.db 全程只读打开」
    # 这种**限定到具体数据源**的说法 —— 那句是真的。
    assert "只读 JSON API" not in doc
    assert "一个字节都不往" not in doc


def test_route_post_写端点是一份可数清单():
    """POST 表的内容本身是契约。

    ## 这个测试原来断言的是相反的事

    2026-08-26 之前它叫 `test_route_post_has_exactly_one_write_endpoint`，
    断言 `route_post` 签名里**没有** db —— 让「POST 不碰审计库」在类型上成立。

    那条约束被显式推翻了：人工定案和验收必须落审计，否则 metrics 里永远躺着
    一片 pending，监工命中率算不出来，而让人能介入正是这个界面存在的理由。

    守卫没有取消，只是换了位置：写审计库的逻辑全部关在 `factory/api_write.py`，
    这里断言动作表就是那三个。加第四个动作时这个测试会红 —— 仍然是刻意的
    提醒，新的写路径要先回答「它写什么、幂等吗、失败留下什么状态」。
    """
    from factory.api import _TASK_ACTIONS

    assert set(_TASK_ACTIONS) == {"resolve", "accept", "rerun"}

    # 不认的路径仍然 404，不是 500
    assert route_post("/api/nope", {}, queue="/tmp")[0] == 404
    # 动作段拼错也是 404，且要给出可选项
    code, body = route_post("/api/task/T-x/frobnicate", {}, queue="/tmp", db=":memory:")
    assert code == 404
    assert "allowed" in body


def test_route_post_没配db时说清是部署问题():
    """db=None 时人工动作报 500 且指向配置，不是让人以为自己参数写错了。"""
    code, body = route_post("/api/task/T-x/resolve", {}, queue="/tmp")
    assert code == 500
    assert "审计库" in body["error"]
    # 提示要指向服务端，否则运维会去查前端
    assert "serve_api" in body["hint"]


def test_route_post_不带动作段的路径不当成动作():
    """`POST /api/task/T-x`（GET 详情的形状）不该被 rpartition 拆出空 task_id。"""
    code, body = route_post("/api/task/T-x", {}, queue="/tmp", db=":memory:")
    assert code == 404
    assert "没有动作段" in body["error"]


def test_submit_writes_journal_line(empty_queue):
    """收下的投递在 log/ 里留一行 JSONL，字段够回答「谁投的、什么时候」。"""
    import json

    submit_task({"task_id": "T-good-one", "yaml": GOOD_SUBMIT}, empty_queue)

    lines = [
        json.loads(line)
        for p in (empty_queue / "log").glob("*.jsonl")
        for line in p.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "submit"
    assert rec["outcome"] == "accepted"
    assert rec["task_id"] == "T-good-one"
    assert rec["source"] == "api"
    assert rec["ts"] > 0


def test_rejected_submit_is_also_logged_with_reasons(empty_queue):
    """被拒的也留痕，且带 why 全文。

    没有这条，「我明明投过那个任务」和「我以为我投过」事后完全同形 ——
    inbox 里都没有，日志里都没有。
    """
    import json

    task_id, body = BAD_SUBMITS["no-acceptance"]
    submit_task({"task_id": task_id, "yaml": body}, empty_queue)

    recs = [
        json.loads(line)
        for p in (empty_queue / "log").glob("*.jsonl")
        for line in p.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["outcome"] for r in recs] == ["rejected"]
    assert recs[0]["why"], "拒了但没记原因"
    assert any("no-acceptance" in w for w in recs[0]["why"])


def test_journal_failure_does_not_break_a_valid_submit(empty_queue, monkeypatch):
    """日志写不进去时投递照常成功。

    日志是观测手段，不是任务的一部分。磁盘满了不该让一次合法投递失败 ——
    反过来（投递成功但没日志）是可接受的降级，Journal 自己往 stderr 抱怨。
    """
    import factory.backlog.journal as journal_mod

    def boom(self, kind, **fields):
        raise OSError("disk full")

    monkeypatch.setattr(journal_mod.Journal, "event", boom)
    # _log_submit 里 import 的是模块属性，patch 到类上就够。
    code, payload = submit_task(
        {"task_id": "T-good-one", "yaml": GOOD_SUBMIT}, empty_queue)

    assert code == 200, payload
    assert (empty_queue / "inbox" / "T-good-one.yaml").is_file()


# ---------- /api/supervisors：监工命中率两层 ----------
#
# 这一层的测试重点不是「数字对不对」（metrics 自己的测试管那个），而是
# 「API 有没有把 metrics 的语义翻错」。翻错一次的代价是拿着看板做反向决策：
# 把一个从没误报的监工当成每次都误报的删掉。


def test_supervisor_stats_gives_both_layers(audit_db):
    """两层都在，且 role 层按 fired 倒序 —— 最吵的监工排最前面。"""
    out = supervisor_stats(audit_db)
    assert set(out) == {"roles", "gates"}
    fired = [r["fired"] for r in out["roles"]]
    assert fired == sorted(fired, reverse=True)


def test_supervisor_precision_counts_only_adjudicated(audit_db):
    """precision 的分母是 TP+FP，不是 fired。

    fixture 里 regression 触发 8 次（前 8 轮 FAIL）且全部 reworked，
    也就是 8 个真阳 0 个误报 —— precision 必须是 1.0。若分母误用 fired，
    未定案的轮次会把它稀释成小于 1 的数，看板上就成了「这监工不太准」。
    """
    roles = {r["role"]: r for r in supervisor_stats(audit_db)["roles"]}
    reg = roles["regression"]
    assert reg["fired"] == 8
    assert reg["true_positives"] == 8
    assert reg["false_positives"] == 0
    assert reg["precision"] == 1.0


def test_never_fired_supervisor_has_null_precision(audit_db):
    """一次都没触发 → precision 是 None，不是 0。

    0 在前端会渲染成 0%，读起来是「每次都误报」。这两个结论相反：
    前者该考虑这道判据是否多余，后者该马上关掉它。所以必须区分。

    这里显式补一个只 PASS 过的角色（scope）而不是指望 fixture 里有 ——
    supervisor_metrics 只统计审计里出现过的 role，没记过的角色根本不会
    出现在结果里，那样断言到的是空列表，测试永远为真却什么都没验。
    """
    store = AuditStore(audit_db)
    attempt_id = store.open_attempt(
        task_id=TASK, spec_ref=[], oracle_class=OracleClass.A,
        class_reason="no rule matched -> default A",
        harness="claude_code", harness_version="2.1.234", model="haiku",
    )
    store.record_verdict(attempt_id, role=SupervisorRole.SCOPE,
                         verdict=Verdict.PASS, claims=[])
    store.finalize(attempt_id, Resolution.MERGED)

    roles = {r["role"]: r for r in supervisor_stats(audit_db)["roles"]}
    scope = roles["scope"]
    assert scope["fired"] == 0
    assert scope["passed"] == 1
    assert scope["precision"] is None


def test_supervisors_route_is_200_and_json_shaped(audit_db, queue):
    """路由层也要通：契约里 /api/supervisors 回 200 且没有 error 键。"""
    code, payload = route("/api/supervisors", db=audit_db, queue=queue)
    assert code == 200
    assert "error" not in payload
    assert isinstance(payload["roles"], list)
    assert isinstance(payload["gates"], list)


def test_supervisors_survives_empty_audit_db(tmp_path, queue):
    """空审计库不该抛，回两个空列表。

    演示环境刚建库时就是这个状态。抛异常会让整个看板 500，
    而正确表现是「还没有数据」。
    """
    code, payload = route("/api/supervisors", db=tmp_path / "fresh.db", queue=queue)
    assert code == 200
    assert payload == {"roles": [], "gates": []}
