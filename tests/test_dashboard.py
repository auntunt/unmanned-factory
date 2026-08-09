"""看板的测试。

这一层是**只读的展示**，所以测的东西和别处不一样：不测它拦不拦得住什么，
测的是「它有没有把一个不成立的世界渲染成一张看着没问题的页面」。这个项目里
反复吃过的亏是「空表和干净的表长得一样」，页面比代码更容易犯这个错 ——
一张空表在浏览器里是安静的。
"""

from __future__ import annotations

import json
import re
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
    ACTIONS,
    FAULT_CLAIMS,
    GATE_CLAIMS,
    MANUAL_HOURS,
    _e,
    _hours,
    _origin_ok,
    build_demo,
    build_demo_queue,
    collect,
    export,
    perform,
    queue_state,
    render,
    state_payload,
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


def test_upstream_fault_stays_in_the_merge_rate_denominator(tmp_path):
    """502 打回的 attempt **留在**合并率分母里 —— 和 gate3 排掉它恰好相反。

    这条是在修完「502 污染三层数字」之后特意加的，防的是过度修正：把 502 从
    gate_hits、监工命中率、gate3 分子里排掉都是对的，顺手把这里也排掉就错了。
    两个数问的不是同一件事 —— gate3 问「验收条件够不够机器可判定」，502 对那件
    事一个字都没说；这个数问「端到端要几次才出货」，502 是那个「几次」的真实
    组成部分。

    排掉的代价：网关九成时间挂着的日子会显示 100% 合并率，和上游完美的日子长得
    一模一样，而偏差方向又是「系统很健康」。所以这里必须是 50%。

    非空基线在分子上：那次 merge 是真的，`merge_rate` 恒返回 0.0 也过不了。
    """
    store, db = _store(tmp_path)

    # attempt#1：真跑起来过（派发过），撞上 502 被打回。
    fault = _attempt(store)
    _result(store, fault, cost=0.0)
    store.record_verdict(
        fault, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{"check": "harness", "command": "claude_code",
                 "expected": "exit_status ok", "got": "API Error: 502"}],
    )
    store.finalize(fault, Resolution.REWORKED)

    # attempt#2：重试，合并。链路处理得完全正确，但它确实花了两次。
    ok = _attempt(store)
    _result(store, ok)
    store.finalize(ok, Resolution.MERGED)

    sm = collect(db)
    assert len(sm.dispatched) == 2, "502 那次是真派发过的，不许当成没跑"
    assert sm.merge_rate == 0.5
    # 而「那一半是谁的锅」由紧挨着的故障计数说清楚，不靠改分母来表达。
    assert dict(sm.fault_hits) == {"harness": 1}
    assert dict(sm.gate_hits) == {}


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


def test_upstream_fault_is_not_counted_or_shown_as_a_gate_hit(tmp_path):
    """网关 502 不许渲染成「闸门拦下」。

    一次真跑批抓到的：上游回 502，attempt 落成 `reworked` + 红色「闸门 harness」。
    于是一条处理得完全正确的链路（打回 → 重试 → 合并）在页面上长成「模型写错了、
    被闸门拦下」—— 客户读到两件都不成立的事。同一个形状在 P0 判据上踩过一次。

    第二个代价在数上：harness 混进 gate_hits，「闸门拦下 N 次」就掺进一堆 502。
    虚高的方向恰好对我们有利，这种偏差没人会来纠，所以必须钉住。

    非空基线：同一次 attempt 里放一条真闸门 claim，证明这条路径本来就在数
    claim。缺了它，把 gate_hits 整个改成永远返回 {} 也能让下面全绿。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store)
    _result(store, aid)
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[
            {"check": "harness", "command": "claude_code",
             "expected": "exit_status ok",
             "got": "error: API Error: 502 Upstream request failed"},
            {"check": "shadow-code", "command": "", "expected": "",
             "got": "被 .gitignore 挡住的 a.py"},   # 基线：真闸门照样要数
        ],
    )
    sm = collect(db)
    assert dict(sm.gate_hits) == {"shadow-code": 1}      # 502 不在里面
    assert dict(sm.fault_hits) == {"harness": 1}         # 但也没被丢掉
    assert state_payload(sm, queue_state(None))["gate_hits_n"] == 1
    assert state_payload(sm, queue_state(None))["fault_hits_n"] == 1

    # 上面那一轮同时有真闸门，页面上「闸门」优先说 —— 那是对的（人该先去看
    # 那个 shadow-code）。所以「故障怎么说」要另起一轮只有 502 的来验。
    solo = _attempt(store, task_id="T-502")
    _result(store, solo)
    store.record_verdict(
        solo, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{"check": "harness", "command": "claude_code",
                 "expected": "exit_status ok", "got": "API Error: 502"}],
    )
    page = render(collect(db), db=db)
    assert "工具/上游故障" in page
    assert "不是代码问题" in page
    # 而那一轮不许出现「闸门 harness」字样 —— 它一个闸门都没触发
    assert "闸门 harness" not in page


def test_gate_claims_matches_dispatcher_source(tmp_path):
    """GATE_CLAIMS 的键必须和 dispatcher 里真实的 `_blocked(...)` 名字对上。

    这张表是手维护的（刻意不从源码正则抽 —— 抽取会让展示层依赖另一个模块的
    源码排版，改个换行就静默变空）。手维护的代价是会漂移：新加一道闸门而忘了
    加这一行，页面上那道闸门就**根本不存在**，而页面看起来完全正常。所以拿
    源码当事实来源做一次对账。

    只查「dispatcher 有、表里没有」这个方向。反方向不查：表里可以留已经下线
    的闸门名，让历史库里的旧 claim 仍然显示得出来。

    对账对的是**两张表的并集**（GATE_CLAIMS ∪ FAULT_CLAIMS）：一个名字必须被
    认识，但它算闸门还是算工具故障是另一件事 —— 那件事由下面那条断言单独钉，
    合在一起会让「分类搬错了」和「名字漏了」报同一个错。
    """
    import re

    src = (Path(__file__).resolve().parent.parent
           / "factory" / "dispatcher.py").read_text(encoding="utf-8")
    # `self._blocked("name", ...)` / `self._blocked(\n    "name",`
    names = set(re.findall(r'_blocked\(\s*\n?\s*"([a-z0-9-]+)"', src))
    assert names, "一个都没抽到 —— 正则和 dispatcher 的写法脱节了，别信这条绿"
    missing = names - set(GATE_CLAIMS) - set(FAULT_CLAIMS)
    assert not missing, f"dispatcher 有这些闸门但看板不认识：{sorted(missing)}"


def test_evidence_table_shows_supervisor_reasons_not_only_gates(tmp_path):
    """证据表要收**所有** claim，不只是机制闸门那几个。

    只收 gate_claims 时，三个真实的打回理由在页面上根本不存在：
    `no-checks-defined`（任务没有可执行判据）、`declared-paths-scope`
    （改到声明之外的文件）、`post-diff-grading`（按真实 diff 重分级后升级）。
    页面此前只在轮次那行写「监工 regression」—— 说了谁反对，没说为什么，
    而演示时「为什么被打回」是第一个被问到的问题。

    非空基线：同一次 attempt 里放一条真闸门 claim。它在旧实现里也显示，所以
    只断言它在，等于什么都没测。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store)
    _result(store, aid)
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[
            {"check": "shadow-code", "command": "", "expected": "",
             "got": "基线：闸门 claim 旧实现也显示"},
            {"check": "no-checks-defined", "command": "",
             "expected": "至少一条廉价客观裁判",
             "got": "任务没有定义任何 check，不能判为通过"},
            {"check": "declared-paths-scope", "command": "",
             "expected": "只改 slug.py", "got": "还改了 deploy.sh"},
        ],
    )
    page = render(collect(db), db=db)
    assert "基线：闸门 claim 旧实现也显示" in page      # 基线
    assert "任务没有定义任何 check" in page             # 这条以前看不见
    assert "还改了 deploy.sh" in page                   # 这条也看不见
    # 而它们要被标成「监工」，不是混进闸门 —— 处置完全不同
    assert "no-checks-defined" in page and "监工" in page


def test_gate_and_fault_tables_do_not_overlap():
    """一个 check 名只能属于一张表。

    两边都有的话，同一次故障会被 gate_hits 和 fault_hits 各数一遍 —— 而这两个
    数在页面上是并排显示的，读起来像「拦下 1 次 + 故障 1 次 = 发生了两件事」。
    """
    both = set(GATE_CLAIMS) & set(FAULT_CLAIMS)
    assert not both, f"这些名字同时在两张表里：{sorted(both)}"


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
    # 8 条单轮 + 1 条 D 类 + T-110 的三轮。数字写死是刻意的：示例数据变了要有人
    # 看一眼，而 `len(rows) > 0` 那种写法在示例库退化成一条时也是绿的。
    assert len(rows) == 12
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


# ---------- 任务树 ----------
#
# 树是这一版给客户看的主体。它最容易犯的错和别处一样：把一个不成立的世界画成
# 一张看着没问题的图。所以下面每条测试都盯一个**具体的坏形状**，而不是「树里
# 有没有这个 task_id」—— 后者对「所有节点都平铺」完全免疫。


def _q(tmp_path, **entries) -> str:
    """造一个真的 Backlog 目录。entries: name -> (state, deps)。"""
    from factory.backlog.store import Backlog

    bl = Backlog(tmp_path / "q").ensure()
    for name, (state, deps) in entries.items():
        doc = f"task_id: {name}\nprompt: 干活\nacceptance: 好了\n"
        if deps:
            doc += "depends_on: [" + ", ".join(deps) + "]\n"
        (bl.dir(state) / f"{name}.yaml").write_text(doc, encoding="utf-8")
    return str(tmp_path / "q")


def _depth(page: str, name: str) -> int:
    """`name` 这个节点在树里的嵌套深度。顶层 = 0。

    数 `<details>` 而不是找字符串在不在：一棵**全平铺**的树里每个 task_id 都在，
    所以「在不在」这个判据对「依赖边一条都没连上」完全免疫 —— 而那正是这一段
    最可能坏掉的方式。
    """
    tree = page.split('<div class="tree">')[1]
    depth = 0
    for m in re.finditer(
            r'(<details class="node" open><summary><code>([^<]+)</code>)'
            r"|(</details>)", tree):
        if m.group(3):
            depth -= 1
            continue
        if m.group(2) == name:
            return depth
        depth += 1
    raise AssertionError(f"树里没有 {name}")


def test_tree_degrades_to_a_flat_list_without_deps(tmp_path):
    """没有依赖时树退化成一排根，**不是一片白**。

    刚起步、还没人写 depends_on 的仓库是常态。那时候页面上该是三个平铺的
    任务，而不是「树：（空）」—— 后者会被读成「系统没认到任何任务」。
    """
    store, db = _store(tmp_path)
    for t in ("T-1", "T-2", "T-3"):
        _result(store, _attempt(store, task_id=t))
    page = render(collect(db), db=db, qs=queue_state(None))
    for t in ("T-1", "T-2", "T-3"):
        assert _depth(page, t) == 0
    assert "没给 <code>--queue</code>" in page


def test_tree_actually_nests_on_the_dependency_edge(tmp_path):
    """B 依赖 A 时，B 必须**嵌在 A 里面**。

    非空基线：三个任务全都在页面上。所以「B 出现了」不等于「边连上了」——
    这条测试量的是深度。
    """
    store, db = _store(tmp_path)
    for t in ("T-a", "T-b", "T-c"):
        _result(store, _attempt(store, task_id=t))
    q = _q(tmp_path, **{"T-a": ("done", ()), "T-b": ("inbox", ("T-a",)),
                        "T-c": ("inbox", ("T-b",))})
    page = render(collect(db), db=db, qs=queue_state(q))
    assert _depth(page, "T-a") == 0
    assert _depth(page, "T-b") == 1
    assert _depth(page, "T-c") == 2


def test_rounds_are_in_ascending_order(tmp_path):
    """一个任务的每一轮按 attempt_no **升序**。

    倒序（最新在上）在页面上完全说得通，所以这条必须钉住方向：编码→测试→
    打回→再编码是从上往下读的故事，反过来讲的是另一个故事，而两张表都好看。
    """
    store, db = _store(tmp_path)
    for _ in range(3):
        _result(store, _attempt(store, task_id="T-1"))
    page = render(collect(db), db=db, qs=queue_state(None))
    seen = re.findall(r"第 (\d+) 轮", page)
    assert seen == ["1", "2", "3"]


def test_a_task_with_no_attempts_says_so_instead_of_blank(tmp_path):
    """队列里有、审计库里没跑过的任务，节点下要写明白。

    留白会被读成「这一轮什么都没发生」，而它的真实含义是「这个任务还没派发」
    或者「派发记在另一个库里」—— 两种处置完全不同。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store, task_id="T-a"))
    q = _q(tmp_path, **{"T-a": ("done", ()), "T-b": ("inbox", ("T-a",))})
    page = render(collect(db), db=db, qs=queue_state(q))
    assert _depth(page, "T-b") == 1
    assert "还没有 attempt" in page


def test_waiting_marker_only_on_entries_that_are_actually_waiting(tmp_path):
    """「等 X」只许挂在 inbox 条目上。

    前置未合并这件事对每个状态都算得出来，所以一个已经进 done/ 的条目也带着
    一串 missing。把它渲染出来，页面就在说一个已经交付的任务正卡着等人 ——
    客户看树是在找卡住的地方，这种假的「卡住」比不显示更贵。

    非空基线：同一个前置、同一份 missing，inbox 那侧必须有标记。缺了它，
    这条断言在「标记功能整个没接上」时同样绿。
    """
    store, db = _store(tmp_path)
    q = _q(tmp_path, **{
        "T-p": ("inbox", ()),          # 前置没合并，所以下面两个都缺 T-p
        "T-wait": ("inbox", ("T-p",)),
        "T-shipped": ("done", ("T-p",)),
    })
    qs = queue_state(q)
    miss = {e.name: e.missing for e in qs.entries}
    assert miss["T-wait"] == ("T-p",)          # 基线：两边算出来的一样
    assert miss["T-shipped"] == ("T-p",)
    page = render(collect(db), db=db, qs=qs)
    assert page.count('<span class="wait">等 T-p</span>') == 1
    # 而聚合口径本来就只数 inbox，两边不许分叉
    assert [n for n, _ in qs.waiting] == ["T-wait"]


def test_cycle_in_the_queue_does_not_swallow_nodes(tmp_path):
    """成环的条目不许从树上**静默消失**。

    环让自顶向下的遍历一个节点都到不了。漏掉三个任务和这三个任务不存在，
    在页面上长得一样 —— 所以漏掉的必须补在后面并说明原因。
    """
    store, db = _store(tmp_path)
    q = _q(tmp_path, **{"T-x": ("inbox", ("T-y",)), "T-y": ("inbox", ("T-x",))})
    page = render(collect(db), db=db, qs=queue_state(q))
    assert "依赖成环" in page
    for t in ("T-x", "T-y"):
        assert f"<code>{t}</code>" in page


def test_diamond_node_is_marked_not_silently_duplicated(tmp_path):
    """菱形依赖里那个共同后继只展开一次，第二次标出来。

    悄悄画两遍会让「一个任务」在页面上看着像「两个任务」，而客户是数节点的。
    """
    store, db = _store(tmp_path)
    q = _q(tmp_path, **{
        "T-a": ("done", ()), "T-b": ("inbox", ("T-a",)),
        "T-c": ("inbox", ("T-a",)), "T-d": ("inbox", ("T-b", "T-c")),
    })
    page = render(collect(db), db=db, qs=queue_state(q))
    assert page.count("已在上面展开过") == 1
    assert page.count("<code>T-d</code>") == 2


def test_queue_read_failure_is_shown_not_swallowed(tmp_path):
    """队列读不出来要**说出来**，不能安静地画一棵没有边的树。

    读不到队列和队列是空的长得一样，而前者意味着这一页在说谎（它会把「等前置」
    显示成 0）。
    """
    bad = tmp_path / "not-a-dir"
    bad.write_text("我不是目录", encoding="utf-8")
    qs = queue_state(str(bad))
    assert qs.error, "读一个文件当队列目录必须失败并留下原因"
    store, db = _store(tmp_path)
    page = render(collect(db), db=db, qs=qs)
    assert "读队列" in page and _e(qs.error) in page


# ---------- 人力账 ----------


def test_labour_ledger_labels_the_assumption(tmp_path):
    """「省下的人力」这一栏必须带**假设**字样。

    这一页唯一能造成真实损害的失效方式是：把一个我们拍的数字显示成实测值，
    然后客户拿它做决定。所以判据不是「这个数算得对」，是「它旁边写着这是假设」。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store, task_id="T-1"))
    page = render(collect(db), db=db, qs=queue_state(None))
    ledger = page.split("<h2>成本与人力账</h2>")[1].split("<h2>")[0]
    assert "人工填的假设" in ledger      # 底下那句说明
    # 但说明句不够 —— 判据必须落在**每一张卡片自己的标签上**。
    # `"假设" in ledger` 会被底下那句说明满足，于是卡片上的标注被拿掉时它照样
    # 绿：而客户看的是卡片，不是卡片下面那行小字。第一版变异跑出来就是这条存活的。
    labels = re.findall(r'<div class="l">([^<]*)</div>', ledger)
    assert len(labels) == 6, labels
    assumed = [l for l in labels if "假设" in l]
    measured = [l for l in labels if "实测" in l]
    # 六张卡片一张不漏地表明了自己是哪一类。少一张就有一个数字身份不明，
    # 而身份不明的数字会被默认当成实测的。
    assert len(assumed) == 2, labels
    assert len(measured) == 4, labels


def test_manual_hours_is_adjustable_and_changes_the_number(tmp_path):
    """估时可改，而且改了数字真的动。

    只测「参数被解析了」不够：一个解析完不用的实现也是绿的。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store, task_id="T-1"))
    sm = collect(db)
    a = render(sm, db=db, qs=queue_state(None), manual_hours=2.0)
    b = render(sm, db=db, qs=queue_state(None), manual_hours=8.0)
    assert a != b
    assert "8 h" in b and "8 h" not in a


@pytest.mark.parametrize("query,want", [
    ("/?manual_hours=4", 4.0),
    ("/?manual_hours=0.5", 0.5),
    ("/", MANUAL_HOURS),
    ("/?manual_hours=abc", MANUAL_HOURS),   # 非法值回落，不 500
    ("/?manual_hours=-3", MANUAL_HOURS),    # 负数会算出「省下负数小时」，纯噪音
    ("/?manual_hours=0", MANUAL_HOURS),
    ("/?manual_hours=99999", MANUAL_HOURS),
])
def test_hours_query_parsing(query, want):
    assert _hours(query) == want


# ---------- /state.json ----------


def test_state_json_is_valid_json_and_has_no_free_text(tmp_path):
    """`/state.json` 里只有聚合数字和 task_id。

    整页 HTML 里有 prompt / class_reason / claims 的 got，那是因为要人主动打开
    看。一个 3 秒被拉一次的端点不该背同样的东西 —— 它会进浏览器缓存、进代理
    日志，而里面可能是仓库内容甚至凭据。
    """
    store, db = _store(tmp_path)
    aid = _attempt(store, task_id="T-1",
                   class_reason="密码是 hunter2，token=sk-ant-secret")
    _result(store, aid)
    sm = collect(db)
    # 非空基线：先证明这段文字**真的到了这一层手里**。少了这一步，
    # 「JSON 里没有 hunter2」在一个空库上同样是绿的 —— 而空库什么都证明不了。
    # （`sk-ant-...` 在写入边界就被 redact 掉了，所以基线只能拿 hunter2 立。）
    assert "hunter2" in sm.rows[0].class_reason

    payload = state_payload(sm, queue_state(None))
    blob = json.dumps(payload, ensure_ascii=False)
    json.loads(blob)  # 合法 JSON
    for leak in ("hunter2", "sk-ant", "密码"):
        assert leak not in blob, f"/state.json 里漏了 {leak}"


def test_state_json_reports_who_waits_on_whom(tmp_path):
    """「谁在等谁」必须出得来，而且不是空的。

    非空基线：队列里两个条目，一个能跑一个在等。空列表和「没有依赖」长得一样。
    """
    store, db = _store(tmp_path)
    q = _q(tmp_path, **{"T-a": ("inbox", ()), "T-b": ("inbox", ("T-a",))})
    payload = state_payload(collect(db), queue_state(q))
    assert payload["waiting"] == [{"task": "T-b", "missing": ["T-a"]}]
    assert payload["waiting_n"] == 1
    assert payload["q_inbox"] == 2


def test_live_numbers_are_rendered_server_side(tmp_path):
    """会动的那几个数字，**初值直接在 HTML 里**，不靠 JS 填。

    静态导出（`--once`）里 `/state.json` 根本不存在。靠 JS 填的话那份发出去的
    HTML 上是一排空格 —— 而它长得像「这批什么都没跑」。
    """
    store, db = _store(tmp_path)
    q = _q(tmp_path, **{"T-a": ("inbox", ()), "T-b": ("inbox", ("T-a",))})
    page = render(collect(db), db=db, qs=queue_state(q))
    assert '<b data-live="q_inbox">2</b>' in page
    assert '<b data-live="waiting_n">1</b>' in page
    # 工具故障也在横条上：一次上游宕机不上横条，就和一切正常长得一样
    # （闸门 0、合并数不动），而人是靠这条横条决定要不要去看一眼的。
    assert '<b data-live="fault_hits_n">0</b>' in page


# ---------- 表单：两道限制 ----------
#
# 这三个表单是这一页唯一的写路径：它们会花钱调模型、会往审计库写 resolution。
# 本机浏览器上**任何**网页都能往 127.0.0.1:8787 POST 一个表单（跨域限制拦的是
# 读响应，不是发请求）。所以下面两道检查是必须的，且都 fail-closed。


@pytest.mark.parametrize("headers,ok", [
    ({"Origin": "http://127.0.0.1:8787"}, True),
    ({"Origin": "http://localhost:8787"}, True),
    ({"Referer": "http://127.0.0.1:8787/"}, True),
    ({}, False),                                  # 两个头都没有 → 拒
    ({"Origin": "http://evil.example"}, False),
    ({"Origin": "https://127.0.0.1.evil.example"}, False),  # 后缀伪装
    ({"Referer": "http://192.168.1.9:8787/"}, False),
    ({"Origin": "null"}, False),                  # sandboxed iframe 的 Origin
])
def test_origin_check_is_fail_closed(headers, ok):
    """判据是白名单，不是黑名单。

    「两个头都没有就放行」是最容易写出来的版本，而它等于没有这道检查 ——
    构造一个不带 Origin 的 POST 是最容易的事。
    """
    assert (_origin_ok(headers) == "") is ok


def test_export_has_no_forms(tmp_path):
    """导出的静态 HTML 里**没有表单**。

    一份发出去的 HTML 上摆着「提需求」按钮，按下去只会静默失败（它 POST 到
    收件人自己的 127.0.0.1）—— 那比没有按钮更糟。
    """
    store, db = _store(tmp_path)
    _result(store, _attempt(store))
    out = tmp_path / "o.html"
    export(db, out)
    page = out.read_text(encoding="utf-8")
    assert 'class="act"' not in page
    assert "name=\"token\"" not in page
    # 但会动的数字和树照样在 —— 导出的页面不是一张残页。
    assert "data-live=" in page and 'class="tree"' in page


def test_actions_whitelist_rejects_anything_else(tmp_path):
    """未知路径不进任何分支。

    「凡 POST 都试着调一下」会让 `/prd/../x` 这种路径落进一个没人想过的分支。
    """
    store, db = _store(tmp_path)
    out = perform("/../etc/passwd", {}, db=db, queue=None)
    assert "不认识的动作" in out
    assert set(ACTIONS) == {"/prd", "/override", "/defect"}


def test_perform_prd_without_queue_refuses(tmp_path):
    """没给 `--queue` 时网页提需求要**明确拒绝**。

    落到默认 `tasks/` 目录会让页面说「已写入」而什么都没进队 ——
    那是最难查的一种成功。
    """
    store, db = _store(tmp_path)
    out = perform("/prd", {"text": "加个重试"}, db=db, queue=None)
    assert "无处可入队" in out
    assert "已写入" not in out


def test_perform_renders_the_exception_instead_of_a_blank(tmp_path):
    """调用抛异常时把异常渲染出来，不显示一张空表。

    一个空白结果和「入队成功」长得一样，而这两件事的后续动作完全相反。
    """
    store, db = _store(tmp_path)
    # attempt_id 填个不存在的 → _cmd_override 返回 1，页面上必须是红的。
    out = perform("/override", {"attempt_id": "999", "resolution": "merged"},
                  db=db, queue=None)
    assert "bad" in out and "退出码 1" in out


def test_perform_surfaces_the_gate_exit_code_as_a_warning(tmp_path):
    """退出码 3 = 闸门拦下，既不能显示成成功，也不能显示成失败。

    3 是闸门在正常工作。染成红的会让人去查系统，染成绿的会让人以为入队了。
    """
    store, db = _store(tmp_path)
    from unittest.mock import patch

    with patch("factory.cli._cmd_prd", return_value=3):
        out = perform("/prd", {"text": "干点啥"}, db=db, queue=str(tmp_path / "q"))
    assert "warn" in out and "闸门" in out
    assert "成功" not in out


# ---------- 接线：真起服务打真请求 ----------
#
# 上面测的是 `_origin_ok` / `perform` **本身**。一道没接上的闸门和一道接上了的，
# 在单元测试和页面上长得完全一样 —— 这个仓库反复吃过这个亏。所以下面这几条
# 真起 ThreadingHTTPServer、真发 POST，判据是「审计库有没有被改」。


@pytest.fixture
def live(tmp_path):
    """起一个真的 dashboard，返回 (base_url, db, 从页面里抠出来的 token)。

    token 不从任何公开接口拿，从页面 HTML 里抠 —— 那正是浏览器做的事，
    顺带证明了 token 真的渲染进了表单（没渲染进去的话表单永远提交不了）。
    """
    import threading
    import time
    import urllib.request

    from factory import dashboard as d

    store, db = _store(tmp_path)
    aid = _attempt(store, task_id="T-1")
    _result(store, aid)
    port = 8900 + (hash(str(tmp_path)) % 400)
    srv = {}

    def run():
        # 端口占用时换一个：并行跑测试或上一次没退干净都会撞。
        for p in range(port, port + 12):
            try:
                srv["port"] = p
                d.serve(db, port=p, queue=str(tmp_path / "q"))
                return
            except OSError:
                continue

    t = threading.Thread(target=run, daemon=True)
    t.start()
    base = None
    for _ in range(80):
        time.sleep(0.05)
        p = srv.get("port")
        if p is None:
            continue
        try:
            page = urllib.request.urlopen(
                f"http://127.0.0.1:{p}/", timeout=2).read().decode()
        except OSError:
            continue
        base = f"http://127.0.0.1:{p}"
        break
    assert base, "dashboard 没起来"
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    return base, db, token, aid


def _post(base, path, body, **headers):
    import urllib.error
    import urllib.request

    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(f"{base}{path}", data=body.encode(),
                                 headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _resolution(db, aid):
    return str(AuditStore(db).all_attempts()[aid - 1].resolution)


@pytest.mark.parametrize("body,headers,why", [
    ("attempt_id={aid}&resolution=merged",
     {"Origin": "{base}"}, "缺 token"),
    ("token={token}&attempt_id={aid}&resolution=merged",
     {"Origin": "http://evil.example"}, "跨 origin"),
    ("token={token}&attempt_id={aid}&resolution=merged",
     {}, "没有 Origin 头"),
    ("token=wrong-token&attempt_id={aid}&resolution=merged",
     {"Origin": "{base}"}, "token 不对"),
])
def test_post_is_rejected_and_the_db_is_untouched(live, body, headers, why):
    """被拒的 POST 不许改审计库。

    判据是**库里那条 resolution 没变**，不是「返回了 403」—— 一个先执行、
    再返 403 的实现照样是 403。非空基线：库里确实有一条 attempt，
    而且它的 resolution 一开始就不是 merged。
    """
    base, db, token, aid = live
    before = _resolution(db, aid)
    assert before != str(Resolution.MERGED), "基线必须和目标值不同"

    code, page = _post(base, "/override",
                       body.format(aid=aid, token=token),
                       **{k: v.format(base=base) for k, v in headers.items()})
    assert code == 403, why
    assert _resolution(db, aid) == before, f"{why}：库被改了"


def test_a_good_post_actually_writes(live):
    """配对的那一半：token 和 Origin 都对时，它**真的写进去**。

    少了这条，一个「凡 POST 都拒」的实现会让上面四条全绿。
    """
    base, db, token, aid = live
    code, page = _post(base, "/override",
                       f"token={token}&attempt_id={aid}&resolution=merged",
                       Origin=base)
    assert code == 200
    assert _resolution(db, aid) == str(Resolution.MERGED)
    assert "成功" in page


def test_state_json_endpoint_is_served(live):
    """`/state.json` 真的挂上了，而且是合法 JSON。

    轮询脚本拿 404 时是静默失败（它 catch 掉一切）—— 页面上的数字就永远不动，
    而「不动」和「没有新数据」长得一样。
    """
    import urllib.request

    base, db, token, aid = live
    raw = urllib.request.urlopen(f"{base}/state.json", timeout=5).read()
    s = json.loads(raw)
    assert s["attempts"] == 1
    assert "queue" in s and "waiting" in s


def test_oversized_post_is_rejected(live):
    """超大提交直接拒，不读进内存也不往下调。"""
    base, db, token, aid = live
    before = _resolution(db, aid)
    body = f"token={token}&attempt_id={aid}&resolution=merged&text=" + "x" * 200_000
    code, page = _post(base, "/override", body, Origin=base)
    assert code == 413
    assert _resolution(db, aid) == before


def test_manual_hours_reaches_the_page_over_http(live):
    """接线测试：`?manual_hours=` 真的走到了渲染里。

    `_hours()` 单独测过，`render(manual_hours=...)` 也单独测过 —— 但一个把
    两者接起来忘了传的实现，在那两条测试和页面上长得完全一样。判据是同一个
    服务上两个 URL 给出**不同的数字**。第一版变异跑出来就是这条存活的。
    """
    import urllib.request

    base, db, token, aid = live
    default = urllib.request.urlopen(f"{base}/", timeout=5).read().decode()
    tuned = urllib.request.urlopen(
        f"{base}/?manual_hours=9", timeout=5).read().decode()
    assert "9 h" not in default, "基线：默认页面上不该有 9 h"
    assert "9 h" in tuned


def test_any_queue_read_error_is_surfaced(tmp_path, monkeypatch):
    """队列侧抛**任何**异常都要留下 `.error`，不是悄悄返一个空壳。

    上面那条测的是「路径不是目录」，走的是显式预检那一条分支。这条盯的是
    兜底 `except`：一个把异常吞成空 QueueState 的实现会让页面把「读不到队列」
    显示成「队列是空的」，而后者是安静的。
    """
    from factory.backlog.store import Backlog

    q = _q(tmp_path, **{"T-a": ("inbox", ())})

    def boom(self):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(Backlog, "counts", boom)
    qs = queue_state(q)
    assert "磁盘炸了" in qs.error
    assert qs.entries == ()   # 半份数据比没有数据更危险，所以这里是空的

    store, db = _store(tmp_path)
    page = render(collect(db), db=db, qs=qs)
    assert "读队列" in page and "磁盘炸了" in page
