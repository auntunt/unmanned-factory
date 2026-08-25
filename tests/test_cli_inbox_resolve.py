"""收件箱的 CLI 闭环：看见卡住的任务 → 人工定案落审计 → 可选放回重跑。

这一层的测试重点是**免查 attempt_id**。在这之前人工定案要走
`factory show <id>` 抄出 attempt_id 再 `factory override <id> merged` ——
两步之间隔着一次肉眼抄数字，于是没人真的做，metrics 里永远躺着一片
pending，看起来像监工从没被验证过。所以这里每条都从「人手上只有 inbox
打出来的东西」出发。
"""

from __future__ import annotations

import json

from factory.audit.models import OracleClass, Resolution
from factory.audit.store import AuditStore
from factory.backlog.store import BLOCKED, NEEDS_HUMAN, Backlog
from factory.cli import main


def make_task(tmp_path, tid: str):
    p = tmp_path / f"{tid}.yaml"
    p.write_text(f"id: {tid}\ngoal: demo\nacceptance:\n  - x\n", encoding="utf-8")
    return p


def park(b: Backlog, tmp_path, tid: str, outcome: str, note: str = ""):
    """走真实 add/claim/finish 停到终态，不手工摆文件。"""
    added = b.add(make_task(tmp_path, tid))
    claim = b.claim(added)
    assert claim is not None
    return b.finish(claim, outcome, note=note)


def open_attempt(db, tid: str) -> int:
    """在审计库里开一轮 attempt，模拟这个任务真的被派发过。"""
    return AuditStore(db).open_attempt(
        task_id=tid, spec_ref=[f"{tid}.yaml"],
        oracle_class=OracleClass.B, class_reason="test",
        harness="claude", harness_version="0", model="sonnet")


def run(argv: list[str]) -> int:
    return main(argv)


# ---------- inbox ----------

def test_inbox_empty_says_so_instead_of_printing_a_bare_header(tmp_path, capsys):
    """空队列要说「没有等人处理的任务」。

    只打一行表头的话，人分不出「真的没有」和「命令没生效/看错了目录」。
    """
    Backlog(tmp_path / "q").ensure()
    assert run(["inbox", "--queue", str(tmp_path / "q")]) == 0
    assert "没有等人处理的任务" in capsys.readouterr().out


def test_inbox_numbers_rows_newest_first(tmp_path, capsys):
    """最近卡住的排 #1，且序号要打出来 —— resolve 要靠它。

    序号是这条链路的关键：needs-human 里的名字是 T-auth-refresh.3 这种，
    照抄一次要么复制粘贴要么打错。
    """
    b = Backlog(tmp_path / "q").ensure()
    park(b, tmp_path, "T-old", "escalated", note="老的")
    park(b, tmp_path, "T-new", "escalated", note="新的")

    assert run(["inbox", "--queue", str(tmp_path / "q"), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["n"], r["task_id"]) for r in rows] == [(1, "T-new"), (2, "T-old")]


def test_inbox_state_filter(tmp_path, capsys):
    """--state 只看一个目录：blocked 和 needs-human 要人做的事不一样。"""
    b = Backlog(tmp_path / "q").ensure()
    park(b, tmp_path, "T-esc", "escalated")
    park(b, tmp_path, "T-blk", "blocked_hard_gate")

    assert run(["inbox", "--queue", str(tmp_path / "q"),
                "--state", BLOCKED, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["task_id"] for r in rows] == ["T-blk"]


def test_inbox_flags_unreadable_result(tmp_path, capsys):
    """.result.json 坏掉的条目照列，标出来。

    反例：读不出来就跳过 —— 于是目录里 5 个条目只列出 3 个，而人正是因为
    「有东西卡住了」才来看这个列表的。
    """
    b = Backlog(tmp_path / "q").ensure()
    parked = park(b, tmp_path, "T-a", "escalated")
    parked.with_name(parked.name + ".result.json").write_text("{坏", encoding="utf-8")

    assert run(["inbox", "--queue", str(tmp_path / "q")]) == 0
    out = capsys.readouterr().out
    assert "T-a" in out and "缺失或坏了" in out


# ---------- resolve：免查 attempt_id ----------

def test_resolve_by_short_number_finalizes_latest_attempt(tmp_path, capsys):
    """`resolve 1 merged` 要把审计里最后一轮定案掉，不需要人给 attempt_id。

    这是整条链路的核心：人手上只有 inbox 打出来的 #1。要是还得先 show 一遍
    抄 attempt_id，人工定案就不会发生，metrics 的分子永远是 0。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated", note="监工说 check 2 没过")
    aid = open_attempt(db, "T-a")

    rc = run(["resolve", "1", "merged", "--why", "人核过，判据写窄了",
              "--queue", str(q), "--db", str(db)])

    assert rc == 0
    row = AuditStore(db).latest_attempt("T-a")
    assert row is not None and row.id == aid
    assert row.resolution == Resolution.MERGED
    assert row.resolution_note == "人核过，判据写窄了"
    assert "resolution=merged" in capsys.readouterr().out


def test_resolve_picks_the_last_round_not_the_first(tmp_path):
    """多轮 attempt 时定案的是最后一轮。

    反例：挑第一轮的话，人核过的是第 3 轮的 diff，定案却记在第 1 轮头上 ——
    metrics 里第 3 轮永远 pending，而第 1 轮多了一个它没经历过的结论。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated")
    open_attempt(db, "T-a")
    open_attempt(db, "T-a")
    last = open_attempt(db, "T-a")

    assert run(["resolve", "T-a", "reworked", "--queue", str(q),
                "--db", str(db)]) == 0

    store = AuditStore(db)
    rows = store.attempts_for("T-a")
    assert [r.resolution for r in rows] == [
        Resolution.PENDING, Resolution.PENDING, Resolution.REWORKED]
    assert rows[-1].id == last


def test_resolve_default_leaves_task_parked(tmp_path, capsys):
    """默认只定案不动队列 —— merged 的任务不该再跑一遍。

    把「定案」和「重跑」绑在一起是最容易犯的错：多数情况人是核过之后认可
    这一轮，任务已经完成了。自动放回 inbox 等于让它再烧一次 token。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    parked = park(b, tmp_path, "T-a", "escalated")
    open_attempt(db, "T-a")

    assert run(["resolve", "1", "merged", "--queue", str(q), "--db", str(db)]) == 0

    assert parked.is_file()
    assert list(b.dir("inbox").iterdir()) == []
    assert "--requeue" in capsys.readouterr().out


def test_resolve_requeue_does_both(tmp_path):
    """--requeue 时审计和队列都要动，且放回的任务能被认领。"""
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated")
    open_attempt(db, "T-a")

    assert run(["resolve", "1", "reworked", "--why", "改了 checks",
                "--requeue", "--queue", str(q), "--db", str(db)]) == 0

    row = AuditStore(db).latest_attempt("T-a")
    assert row is not None and row.resolution == Resolution.REWORKED
    claim = b.claim_next()
    assert claim is not None and claim.task_id == "T-a"


def test_resolve_moves_the_attempt_out_of_unadjudicated(tmp_path):
    """定案后 metrics 里这一轮必须从 unadjudicated 转到真/假阳性。

    这是「免查 attempt_id」要解决的真正问题：定案落不到审计上，命中率的
    分子就永远是 0，而裁剪监工的决定正建在那个数上 —— 于是所有监工看起来
    都「从没被验证过」，一道都不敢删。
    """
    from factory.audit.models import SupervisorRole, Verdict
    from factory.metrics import supervisor_metrics

    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated", note="acceptance 第 2 条没过")
    store = AuditStore(db)
    aid = open_attempt(db, "T-a")
    store.record_verdict(aid, role=SupervisorRole.SPEC, verdict=Verdict.FAIL,
                         claims=[{"check": "acceptance-2"}])

    before = supervisor_metrics(store)["spec"]
    assert (before.fired, before.unadjudicated, before.false_positives) == (1, 1, 0)

    # 人核过之后认为这一轮其实可以合 → 监工是假阳性
    assert run(["resolve", "1", "merged", "--why", "判据写窄了",
                "--queue", str(q), "--db", str(db)]) == 0

    after = supervisor_metrics(AuditStore(db))["spec"]
    assert (after.fired, after.unadjudicated, after.false_positives) == (1, 0, 1)


def test_resolve_bad_number_says_the_range(tmp_path, capsys):
    """序号越界要说清现在有几个，exit 2。

    「找不到 3」帮不上忙 —— 人不知道是自己数错了还是队列已经被同事清了。
    """
    q = tmp_path / "q"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated")

    assert run(["resolve", "9", "merged", "--queue", str(q),
                "--db", str(tmp_path / "audit.db")]) == 2
    assert "只有 1 个" in capsys.readouterr().err


def test_resolve_unknown_id_lists_the_candidates(tmp_path, capsys):
    """打错任务名时要把现有的列出来，不能只说「找不到」。"""
    q = tmp_path / "q"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-auth-refresh", "escalated")

    assert run(["resolve", "T-auth-refesh", "merged", "--queue", str(q),
                "--db", str(tmp_path / "audit.db")]) == 2
    err = capsys.readouterr().err
    assert "T-auth-refresh" in err


def test_resolve_refuses_broken_result_without_force(tmp_path, capsys):
    """.result.json 坏掉时默认拒绝定案 —— 看不到为什么卡住就没法判断对错。

    反例：照常定案 merged。人根本没看到监工说了什么，这条「人工核过」在
    metrics 里和真的核过长得一模一样，于是假阳性统计被污染。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    parked = park(b, tmp_path, "T-a", "escalated")
    parked.with_name(parked.name + ".result.json").unlink()
    open_attempt(db, "T-a")

    assert run(["resolve", "1", "merged", "--queue", str(q), "--db", str(db)]) == 2
    assert "--force" in capsys.readouterr().err
    assert AuditStore(db).latest_attempt("T-a").resolution == Resolution.PENDING


def test_resolve_without_attempt_warns_but_still_requeues(tmp_path, capsys):
    """审计里没有 attempt 时要说出来，队列动作照做。

    这是正常情况：死锁 park 的任务从没派发过，审计里没有它的轮次。静默成功
    的话人会以为 metrics 里该出现这条定案，然后花时间找一条不存在的记录。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated")
    AuditStore(db)                      # 建库但不开 attempt

    assert run(["resolve", "1", "reworked", "--requeue",
                "--queue", str(q), "--db", str(db)]) == 0

    assert "没有 T-a 的 attempt" in capsys.readouterr().err
    assert (b.dir("inbox") / "T-a.yaml").is_file()


def test_resolve_strips_park_suffix_when_looking_up_audit(tmp_path):
    """队列里的 T-a.2 要对上审计里的 T-a。

    反例：拿 `T-a.2` 直接查审计 → 查不到 attempt，第二轮的定案静默落空。
    队列文件名带 park 后缀，审计 task_id 不带，这两个命名空间必须显式对齐。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated", note="第一次")
    second = park(b, tmp_path, "T-a", "escalated", note="第二次")
    assert second.name == "T-a.2.yaml"      # park 加了后缀
    open_attempt(db, "T-a")

    assert run(["resolve", "T-a.2.yaml", "merged", "--why", "核过了",
                "--queue", str(q), "--db", str(db)]) == 0

    row = AuditStore(db).latest_attempt("T-a")
    assert row is not None and row.resolution == Resolution.MERGED
    assert row.resolution_note == "核过了"


def test_resolve_why_is_redacted(tmp_path):
    """理由里贴了 token 要脱敏 —— 和其他自由文本字段一个规格。

    人贴报错原文时会连着环境变量一起贴进来。审计库是要长期留存并且给别人
    看的，明文 token 落进去等于泄露。
    """
    q, db = tmp_path / "q", tmp_path / "audit.db"
    b = Backlog(q).ensure()
    park(b, tmp_path, "T-a", "escalated")
    open_attempt(db, "T-a")

    assert run(["resolve", "1", "merged",
                "--why", "报错里 api_key=sk-live-abcdefghijklmnop 其实是环境没配",
                "--queue", str(q), "--db", str(db)]) == 0

    note = AuditStore(db).latest_attempt("T-a").resolution_note
    assert "sk-live-abcdefghijklmnop" not in note
    assert "环境没配" in note


def test_override_why_lands_in_the_same_column(tmp_path):
    """override --why 和 resolve --why 落同一列，show 都读得到。

    两条路径写不同列的话，`show` 要么漏一半理由，要么得判断该读哪个 ——
    同一个事实存两遍然后等它们不一致。
    """
    db = tmp_path / "audit.db"
    aid = open_attempt(db, "T-a")

    assert run(["override", str(aid), "merged", "--why", "事后回填：人核过",
                "--db", str(db)]) == 0

    assert AuditStore(db).latest_attempt("T-a").resolution_note == "事后回填：人核过"
