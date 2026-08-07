"""入口闸门测试。

闸门决定「人看一眼」什么时候可以不做，所以它的判据不能只有真跑过才知道
对不对 —— 一次误放的代价是花钱跑了个没人核对过分级输入的任务。

最不显然的一条：**unclear 不是一票否决**。实测里一句写得挺完整的需求
（acceptance / checks / declared_paths 都齐了）照样被抽出两条 unclear——
连续空格怎么处理、要不要做类型校验。自然语言永远有没穷尽的边界情形，
unclear 一票否决 = 无人路径永不触发，闸门退化成更啰嗦的 `factory prd`。
"""

from __future__ import annotations

from factory.intake.extract import DraftTask
from factory.intake.gate import admit
from factory.intake.guard import GuardFinding


def draft(**kw) -> DraftTask:
    """默认是一份**够格**的草稿。测试各自破坏其中一项。"""
    base = dict(
        task_id="T-ok",
        prompt="加一个 slugify 函数",
        acceptance=("slugify('A B') == 'a-b'",),
        declared_paths=("src/text.py",),
        checks=({"name": "basic", "command": "true"},),
    )
    base.update(kw)
    return DraftTask(**base)


def test_a_complete_draft_is_admitted():
    v = admit(draft())
    assert v.admitted and v.reasons == ()


# ── unclear 的降级 ────────────────────────────────────────────────────────

def test_unclear_alone_does_not_block_when_acceptance_exists():
    """有验收标准时，未穷尽的边界行为由 worker 自定 —— 那正是 acceptance 的作用。"""
    v = admit(draft(unclear=("连续空格怎么办", "要不要类型校验")))
    assert v.admitted
    assert any("acceptance 已划定边界" in w for w in v.warnings)


def test_unclear_blocks_when_there_is_no_acceptance():
    """没有标准兜底时，那些疑问就真的是需要人回答的东西。"""
    v = admit(draft(unclear=("改哪个文件",), acceptance=(), spec_ref=()))
    assert not v.admitted
    assert any("没有验收标准兜底" in r for r in v.reasons)


def test_spec_ref_also_counts_as_a_boundary():
    """引了外部文档条目的任务，标准在文档里，不必写 acceptance。"""
    v = admit(draft(unclear=("边界?",), acceptance=(), spec_ref=("AC-1",)))
    assert v.admitted


# ── 硬性拦截 ──────────────────────────────────────────────────────────────

def test_no_checks_is_blocked():
    """没有 check 的任务在回归监工那里拿 no-checks-defined FAIL，
    三轮全红上人。放它进队 = 确定烧三轮换一句现在就能免费说的话。"""
    v = admit(draft(checks=()))
    assert not v.admitted
    assert any("烧三轮" in r for r in v.reasons)


def test_no_acceptance_and_no_spec_ref_is_blocked():
    v = admit(draft(acceptance=(), spec_ref=()))
    assert not v.admitted
    assert any("验收标准" in r for r in v.reasons)


def test_declared_ops_is_blocked_even_though_dispatcher_would_also_gate_it():
    """不是因为「D 类危险」（那是 dispatcher 的判断），而是因为 ops 是模型
    从自由文本里抽的，**少抽**一个 prod_deploy 没有下游能兜住。"""
    v = admit(draft(declared_ops=("prod_deploy",)))
    assert not v.admitted
    assert any("prod_deploy" in r for r in v.reasons)


def test_guard_findings_block_even_when_the_model_declared_nothing():
    """guard 从原文嗅到但模型没声明 —— 这正是「少抽了」的实例。"""
    v = admit(draft(guard_findings=(
        GuardFinding(op="data_delete", trigger="删掉所有", pattern="删掉.*所有"),
    )))
    assert not v.admitted
    assert any("guard 另外嗅到" in r for r in v.reasons)


def test_every_reason_is_reported_not_just_the_first():
    """草稿会带着 reasons 落到 needs-human。只报第一条会让人来回补三次。"""
    v = admit(draft(checks=(), acceptance=(), spec_ref=(),
                    declared_ops=("force_push",)))
    assert len(v.reasons) == 3


# ── 提示，不拦 ────────────────────────────────────────────────────────────

def test_missing_declared_paths_is_a_warning_not_a_block():
    """intake 明确要求用户没说文件名就留空。拦下来等于绝大多数口述任务都进不来。"""
    v = admit(draft(declared_paths=()))
    assert v.admitted
    assert any("范围监工" in w for w in v.warnings)


def test_admitted_is_derived_from_reasons_not_a_separate_flag():
    """两个字段会不一致。只留一个真相来源。"""
    from factory.intake.gate import Admission
    assert Admission().admitted
    assert not Admission(("因为",)).admitted


def test_the_gate_does_not_grade():
    """闸门在分级之前，判的是「这份声明值不值得拿去分级」。
    它若自己判起类来，就会出现两套分级规则，而 dispatcher 里那套才是硬的。"""
    # 查 import 而不是查全文：文档里提到 GradingEngine 是在解释"这不是我的事"，
    # 那句话该留着。真正会出问题的是它把分级引擎**导进来**用。
    from factory.intake import gate
    assert not hasattr(gate, "GradingEngine")
    assert not hasattr(gate, "OracleClass")
    assert not hasattr(gate, "Grade")


# ── CLI 接线 ──────────────────────────────────────────────────────────────
#
# 闸门判得对还不够：草稿得真的落在正确的目录里，而且不能覆盖已有的同名条目。

import argparse   # noqa: E402

from factory.backlog.store import INBOX, NEEDS_HUMAN, Backlog   # noqa: E402
from factory.cli import _admit_to_queue   # noqa: E402


def ns(queue):
    return argparse.Namespace(queue=str(queue))


def test_an_admitted_draft_lands_in_inbox(tmp_path):
    q = tmp_path / "q"
    rc = _admit_to_queue(draft(), ns(q))
    assert rc == 0
    assert (q / INBOX / "T-ok.yaml").is_file()
    assert Backlog(q).counts()[INBOX] == 1


def test_a_blocked_draft_lands_in_needs_human_and_returns_3(tmp_path):
    """退出码 3 而不是 1：被拦是闸门在正常工作，不是错误。
    但也不能是 0，否则 `prd --queue && loop` 会在空队列上继续跑。"""
    q = tmp_path / "q"
    rc = _admit_to_queue(draft(checks=()), ns(q))
    assert rc == 3
    assert (q / NEEDS_HUMAN / "T-ok.yaml").is_file()
    assert Backlog(q).counts()[INBOX] == 0


def test_a_blocked_draft_is_still_written_to_disk(tmp_path):
    """丢掉的话一次口述需求就白说了，人连差什么都看不到 —— 比要求确认更糟。"""
    q = tmp_path / "q"
    _admit_to_queue(draft(checks=(), acceptance=(), spec_ref=()), ns(q))
    body = (q / NEEDS_HUMAN / "T-ok.yaml").read_text(encoding="utf-8")
    assert "slugify" in body          # prompt 留下来了


def test_a_same_named_draft_does_not_overwrite_the_earlier_one(tmp_path):
    """同一段需求跑两次 task_id 会一样。覆盖掉等于先排的那个静默消失。"""
    q = tmp_path / "q"
    _admit_to_queue(draft(), ns(q))
    _admit_to_queue(draft(prompt="第二次"), ns(q))
    names = sorted(p.name for p in (q / INBOX).glob("*.yaml"))
    assert names == ["T-ok.2.yaml", "T-ok.yaml"]
    first = (q / INBOX / "T-ok.yaml").read_text(encoding="utf-8")
    assert "slugify" in first        # 第一份没被改写


def test_the_queue_directories_are_created_if_missing(tmp_path):
    """cron 里第一次跑时队列目录还不存在。"""
    q = tmp_path / "nope" / "deeper"
    assert _admit_to_queue(draft(), ns(q)) == 0
    assert (q / INBOX).is_dir()
