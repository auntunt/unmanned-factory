"""闸门的误拒率：编码、记账、以及那个数字的边界。

闸门自己永远不知道它拦错了。唯一的信号是人把 needs-human 里的草稿原样
放回 inbox —— 所以这些测试盯的是「那个动作有没有被记下来」和
「记下来的数字能不能拿去改规则」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.backlog.journal import Journal, Rollup
from factory.intake.gate import RULE_CODES, Admission, admit, rule_code


def _draft(**kw):
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class D:
        task_id: str = "T-x"
        prompt: str = "改点东西"
        spec_ref: tuple = ()
        spec_doc: str = ""
        acceptance: tuple = ("能跑",)
        declared_paths: tuple = ("src/a.py",)
        declared_ops: tuple = ()
        checks: tuple = ({"name": "n", "command": "true"},)
        unclear: tuple = ()
        guard_findings: tuple = ()
        cost_usd: float = 0.0

    return D(**kw)


# --------------------------------------------------------------- 编码

def test_every_reason_carries_a_stable_code():
    # 文案会改，统计的键不能跟着漂。没有编码就只能 grep 中文串，
    # 改一次措辞历史数据就断了。
    v = admit(_draft(checks=(), acceptance=(), spec_ref=(),
                     declared_ops=("prod_deploy",)))
    assert "other" not in v.codes
    assert set(v.codes) <= set(RULE_CODES)


def test_codes_line_up_with_reasons():
    # 样例从 `checks=()` 换成 `acceptance/spec_ref 都空`：前者已降级成 warning
    # （契约送达后 worker 自己写判据），拿它当样例这条会测出 codes == ()。
    v = admit(_draft(acceptance=(), spec_ref=()))
    assert len(v.codes) == len(v.reasons)
    assert v.codes == ("no-acceptance",)


def test_every_declared_code_is_reachable():
    # 一条列在 RULE_CODES 里但没有任何草稿能触发的编码 = 报表上永远的 0。
    hit: set[str] = set()
    for kw in ({"unclear": ("?",), "acceptance": (), "spec_ref": ()},
               {"acceptance": (), "spec_ref": ()},
               {"spec_ref": ("AC-1",)},      # 有编号没文档 → dangling-spec-ref
               {"declared_ops": ("prod_deploy",)}):
        hit |= set(admit(_draft(**kw)).codes)

    from factory.intake.guard import GuardFinding
    hit |= set(admit(_draft(guard_findings=(
        GuardFinding(op="data_delete", trigger="把数据删了", pattern="删数据"),
    ))).codes)
    assert hit == set(RULE_CODES)


def test_an_old_reason_without_a_code_reads_as_other():
    # 加编码之前写下的日志不该让整份统计不可用。
    assert rule_code("没有可执行的 check") == "other"
    assert rule_code("") == "other"
    assert rule_code("[] 空的编码不算") == "other"


def test_a_passing_draft_has_no_codes():
    v = admit(_draft())
    assert v.admitted and v.codes == ()


# ---------------------------------------------------------- 误拒率

def _roll(*events) -> Rollup:
    return Rollup(tuple(events))


def _gate(task_id="T-1", admitted=False, codes=("no-checks",)) -> dict:
    return {"kind": "gate", "task_id": task_id,
            "admitted": admitted, "codes": list(codes)}


def _over(task_id="T-1") -> dict:
    return {"kind": "gate_overruled", "task_id": task_id}


def test_no_decisions_means_no_rate_rather_than_zero():
    # 0% 会被读成「一次都没拦错」。没数据和拦得很准是两件事。
    assert _roll().false_reject_rate is None


def test_admitted_drafts_are_not_in_the_denominator():
    # 分母是拦下的次数，不是全部判决：这个数回答「拦的时候拦错了多少」。
    # 混进放行的会把它稀释成一个总是很小、改不动任何规则的数字。
    r = _roll(_gate(admitted=True, codes=()), _gate("T-2"), _over("T-2"))
    assert len(r.gate_blocked) == 1
    assert r.false_reject_rate == 1.0


def test_a_block_nobody_overruled_reads_as_zero():
    r = _roll(_gate("T-1"), _gate("T-2"))
    assert r.false_reject_rate == 0.0


def test_rule_hits_are_counted_per_code_not_per_draft():
    # 一份草稿可能同时命中几条。按草稿数会让「哪条规则最该松」看不出来。
    r = _roll(_gate("T-1", codes=("no-checks", "declared-ops")),
              _gate("T-2", codes=("no-checks",)))
    assert r.gate_rule_hits == {"no-checks": 2, "declared-ops": 1}


def test_rule_hits_are_ordered_by_frequency():
    r = _roll(_gate("T-1", codes=("declared-ops",)),
              _gate("T-2", codes=("no-checks",)),
              _gate("T-3", codes=("no-checks",)))
    assert list(r.gate_rule_hits) == ["no-checks", "declared-ops"]


def test_a_gate_event_without_codes_still_counts_as_a_block():
    r = _roll({"kind": "gate", "task_id": "T-1", "admitted": False})
    assert r.gate_rule_hits == {"other": 1}


def test_the_report_says_the_rate_is_a_lower_bound():
    # 人懒得放回、或者自己改 YAML 重跑 prd 的都不算进来，所以这个数偏低。
    # 用它判「哪条该松」安全，用它判「闸门够准了」不安全 —— 报表得说清楚。
    r = _roll(_gate("T-1"), _over("T-1"))
    blob = "\n".join(r.lines())
    assert "下界" in blob
    assert "100%" in blob


def test_gate_lines_are_absent_when_the_gate_never_ran():
    blob = "\n".join(_roll({"kind": "dispatch", "outcome": "merged"}).lines())
    assert "闸门" not in blob


# ------------------------------------------------- 两个写入点必须真的写

import argparse   # noqa: E402

from factory.backlog.store import INBOX, LOG, NEEDS_HUMAN, Backlog   # noqa: E402
from factory.cli import _admit_to_queue, _cmd_queue   # noqa: E402
from factory.intake.extract import DraftTask   # noqa: E402


def _events(queue: Path, kind: str) -> tuple[dict, ...]:
    return Journal(Backlog(queue).dir(LOG)).tail(limit=50, kind=kind)


def test_a_blocked_draft_writes_its_codes_to_the_journal(tmp_path):
    # 只打 stdout 不够：stdout 在下一次 prd 之后就没了，误拒率无从计算。
    # acceptance/spec_ref 都空 → no-acceptance。DraftTask 默认这两项就是空的，
    # 显式写出来是为了让这条测试的拦截理由摆在明面上。
    draft = DraftTask(task_id="T-blocked", prompt="改点东西",
                      acceptance=(), spec_ref=())
    rc = _admit_to_queue(draft, argparse.Namespace(queue=str(tmp_path)))
    assert rc == 3

    ev = _events(tmp_path, "gate")
    assert len(ev) == 1
    assert ev[0]["admitted"] is False
    assert "no-acceptance" in ev[0]["codes"]


def test_an_admitted_draft_is_also_recorded(tmp_path):
    # 放行的也要记：分子分母都需要，而且「闸门判了多少次」本身就是个数。
    draft = DraftTask(task_id="T-ok", prompt="改点东西",
                      acceptance=("能跑",), declared_paths=("a.py",),
                      checks=({"name": "n", "command": "true"},))
    assert _admit_to_queue(draft, argparse.Namespace(queue=str(tmp_path))) == 0
    ev = _events(tmp_path, "gate")
    assert ev[0]["admitted"] is True and ev[0]["codes"] == []


def test_requeueing_from_needs_human_is_logged_as_an_overrule(tmp_path):
    bl = Backlog(tmp_path).ensure()
    parked = bl.dir(NEEDS_HUMAN) / "T-p.yaml"
    parked.write_text("task_id: T-p\nprompt: x\n", encoding="utf-8")

    rc = _cmd_queue(argparse.Namespace(queue=str(tmp_path), task=[str(parked)],
                                       history=0))
    assert rc == 0
    ev = _events(tmp_path, "gate_overruled")
    assert len(ev) == 1 and ev[0]["task_id"] == "T-p"


def test_a_normal_enqueue_is_not_counted_as_an_overrule(tmp_path):
    # 从别处入队的任务和闸门无关。算进去会让误拒率虚高，
    # 而虚高的误拒率会把规则松成一个不拦任何东西的闸门。
    src = tmp_path / "hand-written.yaml"
    src.write_text("task_id: T-h\nprompt: x\n", encoding="utf-8")
    Backlog(tmp_path).ensure()

    assert _cmd_queue(argparse.Namespace(queue=str(tmp_path), task=[str(src)],
                                         history=0)) == 0
    assert _events(tmp_path, "gate_overruled") == ()


def test_a_draft_parked_then_overruled_gives_a_computable_rate(tmp_path):
    # 拦截理由用 no-acceptance + dangling-spec-ref（有编号没文档）两条，
    # 好让下面 gate_rule_hits 那个断言仍验到「多条编码各记一次」。
    draft = DraftTask(task_id="T-both", prompt="改点东西",
                      acceptance=(), spec_ref=("AC-1",), spec_doc="")
    _admit_to_queue(draft, argparse.Namespace(queue=str(tmp_path)))
    parked = Backlog(tmp_path).dir(NEEDS_HUMAN) / "T-both.yaml"
    _cmd_queue(argparse.Namespace(queue=str(tmp_path), task=[str(parked)],
                                  history=0))

    roll = Rollup(Journal(Backlog(tmp_path).dir(LOG)).tail(limit=50))
    assert roll.false_reject_rate == 1.0
    assert roll.gate_rule_hits == {"no-acceptance": 1, "dangling-spec-ref": 1}


# ------------------------------------- ops 的归属：谁报的，别记到对方账上

from factory.intake.guard import harden_ops   # noqa: E402


def _real(text: str, declared=()) -> Admission:
    """走真的 harden_ops —— 这个 bug 只在并集语义下出现，手搓字段测不出来。"""
    ops, findings = harden_ops(text, declared=declared)
    return admit(_draft(declared_ops=ops, guard_findings=findings))


def test_an_op_only_guard_found_is_not_charged_to_the_model():
    # harden_ops 是并集：guard 扫出的 op 会被塞进 declared_ops。直接读
    # declared_ops 会让同一个 op 记进两条编码，而这两条编码的全部意义
    # 就是分辨「模型自己报的」和「模型漏了 guard 补的」。
    v = _real("把订单数据删除", declared=())
    assert v.codes == ("guard-ops",)


def test_an_op_only_the_model_declared_is_not_charged_to_guard():
    # 「把老数据处理掉」绕着说，词表扫不出来，但模型读得懂。
    v = _real("把老数据处理掉", declared=("data_delete",))
    assert v.codes == ("declared-ops",)


def test_both_codes_fire_when_each_side_found_a_different_op():
    v = _real("把订单数据删除", declared=("prod_deploy",))
    assert set(v.codes) == {"declared-ops", "guard-ops"}


def test_fixing_the_attribution_did_not_loosen_the_block():
    # 归属改了，拦不拦不能变。guard 嗅到的照样进 needs-human。
    for text, declared in (("把订单数据删除", ()),
                           ("把老数据处理掉", ("data_delete",)),
                           ("把订单数据删除", ("prod_deploy",))):
        assert not _real(text, declared=declared).admitted


def test_the_model_declared_ops_appear_in_the_reason_text():
    # 人打开 needs-human 要看到具体是哪个 op，不能只有编码。
    r = "\n".join(_real("把老数据处理掉", declared=("data_delete",)).reasons)
    assert "data_delete" in r


def test_orm_style_table_truncation_is_still_caught():
    """钉住上面那条注释：不要用「后面跟 ( 就是函数」来收紧 truncate。

    ORM 里清表本身就是函数调用，所以「带括号 = 安全」在这个域里不成立。
    收紧会拿两个真·清表换一个良性函数名。
    """
    from factory.intake.guard import scan_ops
    for text in ('调 conn.truncate("orders") 把订单表清掉',
                 "session.truncate(Orders) 清空测试数据",
                 "TRUNCATE TABLE orders"):
        assert "truncate" in {f.op for f in scan_ops(text)}, text


def test_a_benign_function_named_truncate_is_a_known_false_positive():
    # 记下现状而不是掩盖它：这条误报是刻意留的，代价是人看一眼。
    # 哪天真要收紧，是这个测试该改 —— 改之前先看 [guard-ops] 的命中数。
    from factory.intake.guard import scan_ops
    assert "truncate" in {
        f.op for f in scan_ops("加一个 truncate(s, n) 字符串截断函数")}
