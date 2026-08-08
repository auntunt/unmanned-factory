"""悬空 spec_ref 的预派发闸门。

**编号不是标准。** 一份写着 `spec_ref: [AC-1]` 但没有 spec_doc 的任务，
规格监工收到的字面就是那四个字符 —— 它核不了任何 diff。

实测过一次完整的坏路径，值得写下来，因为每一步都「正常工作」：
监工自己看出来了（好），判 fail（合理），但那条 claim 不带 supervisor-
前缀（它不是监工故障，是监工的真实发现），于是 dispatcher 把它当真问题
打回 worker（按设计），而 worker 改不了「AC-1 没有正文」（它连文档在哪都
不知道），三轮烧完升级给人。全链路没有一个组件出错，钱照烧。

所以这道闸门在**派发之前**：adapter 一次都不调，一分钱不花。

这个文件只测闸门。「监工真的收到正文」在 test_dispatcher_four.py，
「编号怎么在文档里查」在 test_spec_doc.py。
"""

from __future__ import annotations

from factory.audit.models import NOT_DISPATCHED, Resolution
from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.task import CheckSpec, Task

from tests.test_dispatcher import AlwaysPass, FakeAdapter, _result


class CountingAdapter(FakeAdapter):
    """记录被调过几次。闸门的全部意义是让这个数字保持 0。"""

    def __init__(self):
        super().__init__([_result()])


def _build(tmp_path):
    store = AuditStore(tmp_path / "a.db")
    adapter = CountingAdapter()
    d = Dispatcher(adapter=adapter, store=store, supervisor=AlwaysPass())
    return d, store, adapter


def _task(**kw):
    base = dict(task_id="T-sr", prompt="加个函数",
                declared_paths=("greet.py",),
                checks=(CheckSpec(name="ok", command="true"),))
    base.update(kw)
    return Task(**base)


# ---------- 拦 ----------

def test_a_ref_without_a_doc_never_reaches_the_adapter(tmp_path):
    """最要紧的一条：**不花钱**。断言 adapter 的调用次数，不只是 outcome。"""
    d, store, adapter = _build(tmp_path)
    rep = d.run(_task(spec_ref=("AC-1",)), tmp_path)

    assert adapter.calls == [], "闸门在派发前，adapter 一次都不该被调"
    assert rep.outcome is Outcome.ESCALATED
    assert rep.rounds == 0
    assert "没有 spec_doc" in rep.escalation_reason


def test_a_ref_the_doc_does_not_define_is_blocked_too(tmp_path):
    """文档给了，但里面没这条 —— 编号写错、或者文档改过。

    这比「没给文档」更需要拦：没给文档是明显的疏漏，写错编号看起来完全正常。
    """
    (tmp_path / "prd.md").write_text(
        "## 验收标准\n- AC-1: 必须返回 str\n", encoding="utf-8")
    d, _store, adapter = _build(tmp_path)
    rep = d.run(_task(spec_ref=("AC-1", "AC-9"), spec_doc="prd.md"), tmp_path)

    assert adapter.calls == []
    assert rep.outcome is Outcome.ESCALATED
    assert "AC-9" in rep.escalation_reason
    assert "AC-1" not in rep.escalation_reason.replace("AC-1:", ""), \
        "只报查不到的那条，别把查到的也算进去"


def test_a_missing_doc_file_is_blocked(tmp_path):
    d, _store, adapter = _build(tmp_path)
    rep = d.run(_task(spec_ref=("AC-1",), spec_doc="nope.md"), tmp_path)
    assert adapter.calls == []
    assert "不存在" in rep.escalation_reason


def test_a_ref_that_matches_a_line_with_no_body_is_blocked(tmp_path):
    """`- AC-1:` 后面什么都没有。命中了行首，但正文是空的。

    这条最像「查到了」—— 编号确实在文档里。但空正文和查不到是同一件事：
    递给监工的还是四个字符。
    """
    (tmp_path / "prd.md").write_text(
        "## 验收标准\n- AC-1:\n- AC-2: 真的有正文\n", encoding="utf-8")
    d, _store, adapter = _build(tmp_path)
    rep = d.run(_task(spec_ref=("AC-1",), spec_doc="prd.md"), tmp_path)
    assert adapter.calls == []
    assert "AC-1" in rep.escalation_reason


# ---------- 记账的形状 ----------

def test_the_blocked_attempt_looks_like_every_other_pre_dispatch_block(tmp_path):
    """预派发拦截不止分级一种，但它们的记账形状必须一样。

    形状不一样的后果是报表上漏掉一整类拦截：`factory metrics` 靠
    harness_version=NOT_DISPATCHED 认「没花钱的 attempt」，靠
    resolution=escalated 认「上人了」。少一个字段，这类拦截就在
    「拦了多少」和「花了多少」两个数字之间凭空消失。
    """
    d, store, _ = _build(tmp_path)
    rep = d.run(_task(spec_ref=("AC-1",)), tmp_path)

    row = store.get(rep.attempt_ids[0])
    # 用常量不用字面量："n/a" 这个值是 metrics 认「没花钱的 attempt」的依据，
    # 两边必须是同一个东西，而不是同一个巧合。
    assert row.harness_version == NOT_DISPATCHED, "没派发就得看得出来没派发"
    assert row.resolution == Resolution.ESCALATED
    assert row.cost_usd == 0
    assert (row.tokens_in, row.tokens_out) == (0, 0)
    assert row.diff_hash in (None, ""), "没派发就没有 diff"

    claims = [c for v in row.supervisors for c in v.claims]
    codes = [c["check"] for c in claims]
    assert "pre-dispatch-spec-ref" in codes, \
        "拦的理由要落进 claim，否则回查时只看到一条没有原因的 escalated"
    claim = claims[codes.index("pre-dispatch-spec-ref")]
    assert "spec_doc" in claim["expected"]
    assert "没有 spec_doc" in claim["got"]
    # 不带 supervisor- 前缀：这不是监工故障，是我们自己在派发前拦的。
    assert not claim["check"].startswith("supervisor-")


def test_the_gate_lets_a_well_formed_task_through(tmp_path):
    """闸门只拦悬空的那种。一律拦死等于把 spec_ref 这个功能关掉。"""
    (tmp_path / "prd.md").write_text(
        "## 验收标准\n- AC-1: 必须返回 str\n", encoding="utf-8")
    d, _store, adapter = _build(tmp_path)
    rep = d.run(_task(spec_ref=("AC-1",), spec_doc="prd.md"), tmp_path)
    assert rep.outcome is Outcome.MERGED
    assert len(adapter.calls) == 1


def test_no_spec_ref_at_all_is_never_touched_by_this_gate(tmp_path):
    """口述任务的形状：只有 acceptance。它没有编号，也就没有可悬空的东西。"""
    d, _store, adapter = _build(tmp_path)
    rep = d.run(_task(acceptance=("返回值是 str",)), tmp_path)
    assert rep.outcome is Outcome.MERGED
    assert len(adapter.calls) == 1
