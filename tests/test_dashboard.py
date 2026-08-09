"""看板的测试。

这一层是**只读的展示**，所以测的东西和别处不一样：不测它拦不拦得住什么，
测的是「它有没有把一个不成立的世界渲染成一张看着没问题的页面」。这个项目里
反复吃过的亏是「空表和干净的表长得一样」，页面比代码更容易犯这个错 ——
一张空表在浏览器里是安静的。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from factory.audit.store import AuditStore
from factory.audit.models import (
    NOT_DISPATCHED,
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.dashboard import (
    GATE_CLAIMS,
    _e,
    build_demo,
    collect,
    export,
    render,
)


def _store(tmp_path) -> tuple[AuditStore, str]:
    db = str(tmp_path / "a.db")
    return AuditStore(db), db


def _attempt(
    store: AuditStore,
    *,
    task_id: str = "T-1",
    oracle_class: OracleClass = OracleClass.A,
    class_reason: str = "有可执行判据",
    model: str = "claude-opus-5",
) -> int:
    return store.open_attempt(
        task_id=task_id,
        spec_ref=["§1"],
        oracle_class=oracle_class,
        class_reason=class_reason,
        harness="claude-code",
        harness_version="1.0",
        model=model,
    )


def _result(store: AuditStore, aid: int, *, cost: float = 0.05) -> None:
    store.record_result(
        aid,
        diff_hash="d" * 8,
        commit=None,
        transcript_path=None,
        tokens_in=100,
        tokens_out=200,
        cost_usd=cost,
        wall_clock_ms=1000,
    )


# ---------- 分母 ----------


def test_merge_rate_is_none_on_empty_db(tmp_path):
    """空库要给 None，不是 0.0。

    0% 长得像「跑了很多次全失败」，而真相是「一次都没跑」。页面上这两件事
    必须能区分，所以判据是 `is None` 而不是 `== 0`。
    """
    _, db = _store(tmp_path)
    sm = collect(db)
    assert sm.total == 0
    assert sm.merge_rate is None


def test_d_class_stays_out_of_the_denominator(tmp_path):
    """D 类硬闸门拦下的从没派发过，不许进合并率的分母。

    进了分母就变成「拦得越多，指标越好看」—— 这个方向的错误在页面上是
    看不出来的，因为数字只会朝好的方向走。
    """
    store, db = _store(tmp_path)
    ok = _attempt(store)
    _result(store, ok)
    store.finalize(ok, Resolution.MERGED)

    # 走真实形状：harness 照样是 adapter 名字，只有 harness_version 是 n/a。
    # 这里如果偷懒把 harness 填成 "n/a"，就会把一个读错字段的实现测成绿的
    # —— 第一版就是这么漏掉的。
    blocked = store.open_attempt(
        task_id="T-2",
        spec_ref=["§2"],
        oracle_class=OracleClass.D,
        class_reason="无可执行判据",
        harness="claude-code",
        harness_version=NOT_DISPATCHED,
        model="claude-opus-5",
    )
    store.finalize(blocked, Resolution.ESCALATED)

    sm = collect(db)
    assert sm.total == 2
    assert len(sm.dispatched) == 1
    # 2 条里 1 条合并，但分母只算派发过的那 1 条 → 100%，不是 50%。
    assert sm.merge_rate == 1.0


# ---------- 插值 ----------


@pytest.mark.parametrize(
    "raw",
    [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "a & b < c",
        "'单引号'",
    ],
)
def test_model_text_cannot_break_out_of_the_page(tmp_path, raw):
    """class_reason 和 claims 是**模型生成的文本**，直接插进 HTML 就是注入点。

    这不是假想威胁：class_reason 由分级 agent 写，claims 里的 got 直接来自
    check 命令的 stdout。两者都能被 worker 影响。所以 `_e()` 是唯一的插值出
    口，且必须带 quote=True —— 只转义 `<>&` 挡不住属性里的 `">`。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store, class_reason=raw)
    _result(store, aid)
    store.record_verdict(
        aid,
        role=SupervisorRole.REGRESSION,
        verdict=Verdict.FAIL,
        claims=[{"check": "gitlink-added", "command": "", "expected": "",
                 "got": raw}],
    )
    page = render(collect(db), db=db)
    assert raw not in page, "原文出现在页面里 —— 有一处插值绕过了 _e()"
    assert _e(raw) in page


def test_escape_covers_quotes():
    """判据盯 quote=True 本身。

    `html.escape(s)` 默认 quote=True，但显式写出来是为了防「有人为了让日志
    好看把它改成 False」—— 那一改不会有任何报错，页面看起来完全正常。
    """
    assert _e('"') == "&quot;"
    assert _e("'") == "&#x27;"
    assert _e("<b>") == "&lt;b&gt;"


# ---------- 闸门表 ----------


def test_zero_hit_gates_render_as_zero_not_blank(tmp_path):
    """十四道闸门一次没触发时，表里要有十四行、每行一个 `0`。

    留空会被读成「这道闸门不存在」。这个项目里「空表和干净的表长得一样」
    已经踩过多次，展示层是它最容易发生的地方。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store))
    page = render(collect(db), db=db)
    for name in GATE_CLAIMS:
        assert f"<code>{name}</code>" in page, f"{name} 没出现在闸门表里"
    assert page.count('class="num dim">0<') >= len(GATE_CLAIMS)


def test_gate_hits_counts_only_known_gates(tmp_path):
    """gate_hits 只数认识的闸门名，不认识的 claim 不许混进这张表。

    监工的普通 claim（比如回归监工报的某个测试失败）和机制闸门的 claim 走
    的是同一张表，靠 check 名字区分。不过滤的话「闸门拦下 N 次」会变成
    「监工报警 N 次」，而这两个数的含义完全不同。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store)
    _result(store, aid)
    store.record_verdict(
        aid,
        role=SupervisorRole.REGRESSION,
        verdict=Verdict.FAIL,
        claims=[
            {"check": "gitlink-added", "command": "", "expected": "", "got": "x"},
            {"check": "head-moved", "command": "", "expected": "", "got": "y"},
            {"check": "pytest", "command": "pytest", "expected": "exit_zero",
             "got": "exit 1"},
        ],
    )
    sm = collect(db)
    assert dict(sm.gate_hits) == {"gitlink-added": 1, "head-moved": 1}


def test_gate_claims_matches_dispatcher_source(tmp_path):
    """GATE_CLAIMS 的键必须和 dispatcher 里真实的 `_blocked(...)` 名字对上。

    这张表是手维护的（刻意不从源码正则抽 —— 抽取会让展示层依赖另一个模块的
    源码排版，改个换行就静默变空）。手维护的代价是会漂移：新加一道闸门而忘了
    加这一行，页面上那道闸门就**根本不存在**，而页面看起来完全正常。所以拿
    源码当事实来源做一次对账。

    只查「dispatcher 有、表里没有」这个方向。反方向不查：表里可以留已经下线
    的闸门名，让历史库里的旧 claim 仍然显示得出来。
    """
    import re

    src = (Path(__file__).resolve().parent.parent
           / "factory" / "dispatcher.py").read_text(encoding="utf-8")
    # `self._blocked("name", ...)` / `self._blocked(\n    "name",`
    names = set(re.findall(r'_blocked\(\s*\n?\s*"([a-z0-9-]+)"', src))
    assert names, "一个都没抽到 —— 正则和 dispatcher 的写法脱节了，别信这条绿"
    missing = names - set(GATE_CLAIMS)
    assert not missing, f"dispatcher 有这些闸门但看板不认识：{sorted(missing)}"


# ---------- 导出 ----------


def test_export_writes_a_self_contained_file(tmp_path):
    """`--once` 的产物要能直接发给别人：不许有任何外部引用。

    外部 CSS/JS/字体在开发机上看着好好的（有网），发到对方手上就退化成一张
    没样式的纯文字页 —— 而这种退化在我这边永远看不到。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store))
    out = tmp_path / "out.html"
    assert export(db, out) == 0

    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    for bad in ("<script src", "<link ", "http://", "https://", "@import"):
        assert bad not in page, f"页面引了外部资源：{bad}"


def test_export_of_empty_db_says_so(tmp_path):
    """空库导出不能是一张安静的空页。

    「还没跑过」和「跑过但什么都没拦下」必须在页面上能区分，这是这个项目里
    反复出现的那个形状在展示层的版本。
    """
    _, db = _store(tmp_path)
    out = tmp_path / "empty.html"
    export(db, out)
    page = out.read_text(encoding="utf-8")
    assert "还没" in page or "—" in page
    # 闸门表在空库上照样是满的十四行 —— 闸门装着这件事不取决于有没有数据。
    for name in GATE_CLAIMS:
        assert name in page


# ---------- 只读 ----------


def test_collect_does_not_write_the_db(tmp_path):
    """看板只读。

    判据用 SQLite 的 data_version（每次有写事务提交就 +1），而不是文件
    mtime —— mtime 的粒度粗到能让一次写混过去。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store))

    def version() -> int:
        c = sqlite3.connect(db)
        try:
            return c.execute("PRAGMA data_version").fetchone()[0]
        finally:
            c.close()

    before = version()
    collect(db)
    render(collect(db), db=db)
    assert version() == before, "看板写了库"


# ---------- 示例数据 ----------


def test_demo_page_says_it_is_a_demo(tmp_path):
    """示例页面必须自己承认是示例。

    这一页唯一真正有害的失效方式，是一张编出来的页面被当成真实跑批结果拿给
    别人看。横幅挂在 render() 里、判据是**库文件名**，不是一个参数 ——
    参数会漏传，而漏传的后果正好是那个有害方向。
    """
    db = build_demo(tmp_path / "demo.db")
    page = render(collect(db), db=db)
    assert "示例数据" in page
    assert "不是真实跑批结果" in page


def test_real_db_has_no_demo_banner(tmp_path):
    """反方向也要钉住：真实库不许挂这条横幅。

    误挂的后果是真实结果被当成编的 —— 危害小，但同一个判据管两边，一起测掉。
    """
    store, db = _store(tmp_path)  # 文件名是 a.db
    _result(store, _attempt(store))
    assert "示例数据" not in render(collect(db), db=db)


def test_demo_refuses_a_non_demo_filename(tmp_path):
    """写非 demo* 的文件名要炸，而不是悄悄写出一张没横幅的假页面。"""
    with pytest.raises(ValueError, match="demo"):
        build_demo(tmp_path / "audit.db")


def test_demo_covers_all_three_adjudication_states(tmp_path):
    """示例数据要让监工表真的有数，而不是整列「数据不足」。

    命中率的分母只算已定案的裁决（REWORKED=真阳性，MERGED/HUMAN_OVERRIDE=
    假阳性，ESCALATED/PENDING=未定案）。第一版示例里报警的全是 escalated，
    于是页面上最该说明问题的一栏是空的 —— 而空栏看起来只像「还没攒够数据」。
    """
    db = build_demo(tmp_path / "demo.db")
    sm = collect(db)
    reg = sm.supervisors["regression"]
    assert reg.true_positives > 0, "没有真阳性 → 命中率是 None"
    assert reg.false_positives > 0, "没有假阳性 → 命中率恒为 100%，不真实"
    assert reg.unadjudicated > 0, "没有未定案 → 看不出「报了警还没人看」这一格"
    assert reg.hit_rate is not None
    assert sm.merge_rate is not None

    # 漏报只能来自「放行 + 事后挂 defect」，没有它那一列永远是 0。
    assert any(m.false_negatives > 0 for m in sm.supervisors.values())


def test_demo_has_a_never_dispatched_row(tmp_path):
    """示例里要有一条 D 类没派发的。

    它在页面上的作用是让「合并率的分母是 8 不是 9」看得出来。少了它，那个
    百分比看不出是怎么算的，而算错了也没人能发现。
    """
    db = build_demo(tmp_path / "demo.db")
    sm = collect(db)
    assert sm.total - len(sm.dispatched) == 1


def test_demo_writes_through_the_real_store(tmp_path):
    """示例库的表结构必须和真实库一致。

    判据：AuditStore 能把它读回来，而且字段都在。绕过 store 拼 SQL 会让示例
    库和真实库漂移 —— 一个演示页显示得出来、真实页显示不出来的字段，比空页
    更难发现。
    """
    db = build_demo(tmp_path / "demo.db")
    rows = AuditStore(db).all_attempts()
    assert len(rows) == 9
    for r in rows:
        assert r.class_reason and r.model and r.oracle_class
        assert r.supervisors, "每条都该有裁决"


# ---------- 监工那一列 ----------


def test_all_pass_shows_how_many_ran_not_just_pass(tmp_path):
    """全过时要打出**跑了几道**，不能只写「全过」。

    这一列为了压宽度做了折叠：只列报警的监工，全过折成「N 过」。折叠本身没
    问题，但如果写死「全过」，那么「四道都跑了且都放行」和「只跑了一道、另外
    三道压根没被调用」在页面上就长得一样 —— 而后者是这个项目里最贵的一类
    bug（一道谁都没接上的闸门，在报表上和接上了完全一致）。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store)
    _result(store, aid)
    # 只跑一道监工
    store.record_verdict(aid, role=SupervisorRole.REGRESSION,
                         verdict=Verdict.PASS, claims=[])
    one = render(collect(db), db=db)

    sub = tmp_path / "sub"
    sub.mkdir()
    store2, db2 = _store(sub)
    aid2 = _attempt(store2)
    _result(store2, aid2)
    for role in (SupervisorRole.REGRESSION, SupervisorRole.SPEC,
                 SupervisorRole.RISK, SupervisorRole.ARCHITECTURE):
        store2.record_verdict(aid2, role=role, verdict=Verdict.PASS, claims=[])
    four = render(collect(db2), db=db2)

    assert "1 过" in one
    assert "4 过" in four
    assert "1 过" not in four, "跑一道和跑四道渲染成了同一个字"


def test_failing_supervisor_is_named(tmp_path):
    """有监工报警时，页面要说出**是哪一道**。

    折叠成「有报警」会让「回归监工报的」和「架构监工报的」看起来一样，而这两
    件事对应完全不同的下一步动作。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store)
    _result(store, aid)
    store.record_verdict(aid, role=SupervisorRole.ARCHITECTURE,
                         verdict=Verdict.FAIL,
                         claims=[{"check": "shadow-code", "command": "",
                                  "expected": "", "got": "x"}])
    store.record_verdict(aid, role=SupervisorRole.SPEC,
                         verdict=Verdict.PASS, claims=[])
    page = render(collect(db), db=db)
    assert "architecture" in page
    assert "2 过" not in page, "有一道报警了却渲染成全过"
