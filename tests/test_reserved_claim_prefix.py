"""模型能不能靠 claim 的 check 字段冒充「监工自己坏了」。

`supervisor-` 是 harness 内部保留前缀：带它的 claim 在
`dispatcher._merge_reports` 里归成 **faults**，走「监工不可用，不打回 worker，
直接升级给人」那条路。而 claim 的 check 是**模型写的字符串**，原样进 coerce。

后果得算清：faults 一样 blocking，所以这不放行坏代码。它把「打回 worker 自己
修」变成「叫人来看监工坏了」—— 白占一次人工，而那其实是一条 worker 改代码就
能修的真发现。P1 判据里「上人平均打回次数」和人工次数都被它污染。

修法是剥前缀而不是判红：claim 内容可能是对的，丢掉它等于丢一条真发现。
"""

from __future__ import annotations

from factory.audit.models import SupervisorRole, Verdict
from factory.dispatcher import Dispatcher
from factory.supervisors.base import SupervisorReport
from factory.supervisors.model_base import (
    SUPERVISOR_ERROR_PREFIX,
    ModelCall,
    coerce_claims,
    report_from_call,
)


def _claim(check: str) -> dict:
    return {"check": check, "expected": "x", "got": "y"}


def _merge(claims, role=SupervisorRole.SPEC, is_last=False):
    r = SupervisorReport(role=role, verdict=Verdict.FAIL, claims=tuple(claims))
    return Dispatcher._merge_reports((r,), is_last=is_last)


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_a_forged_prefix_lands_in_faults() -> None:
    """不经过 coerce 的话，模型写的前缀直接把 claim 变成「监工故障」。"""
    merged = _merge([_claim(f"{SUPERVISOR_ERROR_PREFIX}spec-timeout")])

    assert len(merged.faults) == 1, "分类逻辑变了？"
    assert merged.feedback == ()
    assert merged.blocking, "至少它不放行"


def test_the_hole_costs_a_human_not_a_rework() -> None:
    """faults 和 feedback 的差别就是「叫人」和「打回 worker」的差别。"""
    forged = _merge([_claim(f"{SUPERVISOR_ERROR_PREFIX}spec-timeout")])
    honest = _merge([_claim("S-1 返回值不对")])

    assert forged.faults and not forged.feedback
    assert honest.feedback and not honest.faults


# --- 二、修法：coerce 时剥掉 ----------------------------------------------


def test_coerce_strips_the_reserved_prefix() -> None:
    assert coerce_claims([_claim(f"{SUPERVISOR_ERROR_PREFIX}spec-timeout")])[0][
        "check"
    ] == "spec-timeout"


def test_coerce_strips_a_nested_prefix() -> None:
    """剥一次不够 —— `supervisor-supervisor-x` 剥完还带前缀。"""
    raw = _claim(f"{SUPERVISOR_ERROR_PREFIX}{SUPERVISOR_ERROR_PREFIX}x")
    got = coerce_claims([raw])[0]["check"]
    assert got == "x"
    assert not got.startswith(SUPERVISOR_ERROR_PREFIX)


def test_a_bare_prefix_is_dropped() -> None:
    """剥完什么都不剩的 claim 没有内容可打回，丢掉。"""
    assert coerce_claims([_claim(SUPERVISOR_ERROR_PREFIX)]) == ()


def test_the_content_is_kept_not_discarded() -> None:
    """剥前缀不是丢 claim：它的内容可能是一条真发现。"""
    raw = {
        "check": f"{SUPERVISOR_ERROR_PREFIX}S-1",
        "expected": "返回两数之和",
        "got": "返回的是差",
    }
    out = coerce_claims([raw])[0]
    assert out["expected"] == "返回两数之和"
    assert out["got"] == "返回的是差"


def test_a_normal_check_is_untouched() -> None:
    assert coerce_claims([_claim("S-1 返回值不对")])[0]["check"] == "S-1 返回值不对"


# --- 三、接线：走完 report_from_call 之后进的是 feedback ------------------


def test_a_forged_claim_becomes_a_rework_not_a_fault() -> None:
    call = ModelCall(
        ok=True,
        verdict="fail",
        claims=coerce_claims([_claim(f"{SUPERVISOR_ERROR_PREFIX}spec-timeout")]),
    )
    rep = report_from_call(SupervisorRole.SPEC, call, what="规格监工")
    merged = Dispatcher._merge_reports((rep,), is_last=False)

    assert merged.faults == (), "还是被当成监工故障"
    assert len(merged.feedback) == 1
    assert merged.feedback[0]["check"] == "spec-timeout"


def test_a_real_fault_still_reaches_faults() -> None:
    """harness 自己造的那条必须照旧 —— 它不经过 coerce_claims。"""
    call = ModelCall(ok=False, error_text="timeout after 300s")
    rep = report_from_call(SupervisorRole.SPEC, call, what="规格监工")
    merged = Dispatcher._merge_reports((rep,), is_last=False)

    assert len(merged.faults) == 1
    assert merged.faults[0]["check"].startswith(SUPERVISOR_ERROR_PREFIX)


def test_fail_with_no_claims_still_reaches_faults() -> None:
    """另一条 harness 造的：判 fail 却不说哪儿错。也不经过 coerce。"""
    call = ModelCall(ok=True, verdict="fail", claims=())
    rep = report_from_call(SupervisorRole.SPEC, call, what="规格监工")
    merged = Dispatcher._merge_reports((rep,), is_last=False)

    assert len(merged.faults) == 1
    assert "empty-claims" in merged.faults[0]["check"]
