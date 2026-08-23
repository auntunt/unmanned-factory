"""挂死熔断：连续两次 worker 不产出就上人，不把模型阶梯烧完。

背景（真实派发，audit.db 里可查）：一个任务三轮 haiku→sonnet→opus 全灭，
其中两轮是 `timeout after 900s` —— CLI 起来了但 CPU 0%、卡在 ep_poll、
零网络连接，也不退出。而超时的 attempt 记 $0，`--budget-usd` 拦不住。
总墙钟 2276s，真正的产出来自唯一没挂的那一轮。

模型阶梯是为「能力不够」设计的，环境故障往上换模型只是把同一个问题
烧三遍。这里验熔断确实截断了那个浪费。
"""

from __future__ import annotations

from factory.dispatcher import Outcome
from factory.harness.base import AttemptResult, ExitStatus
from tests.test_dispatcher import (  # 复用现成装配，避免又造一套半真的
    FakeAdapter,
    _dispatcher,
    _result,
    _task,
    store,  # noqa: F401 - pytest fixture
)


def _stalled():
    """一次挂死的 attempt：没有 diff、没有改动、stalled=True。

    changed_paths 必须是空的 —— 挂死的 worker 一个字节都没吐，工作区
    自然没有改动。给它编造改动会让这个测试悄悄测成别的东西。
    """
    return AttemptResult(
        exit_status=ExitStatus.TIMEOUT,
        error_text="stalled: no output for 120s (limit 900s not reached)",
        stalled=True,
        wall_clock_ms=120_000,
    )


def test_two_consecutive_stalls_escalate_without_burning_ladder(
        store, tmp_path, spec_doc):  # noqa: F811
    """连续两次挂死 → 第二轮就上人，不跑第三轮。"""
    spec_doc(tmp_path)  # spec_ref 非空的任务必须有 PRD，否则预闸门先拦下
    adapter = FakeAdapter([_stalled(), _stalled(), _result()])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.ESCALATED
    # 关键断言：停在第 2 轮。原来会一路跑到 max_rounds=3，
    # 把 opus 也喂给同一个环境故障。
    assert report.rounds == 2
    assert len(adapter.calls) == 2, "第三轮不该派发"
    # 模型名不写死：阶梯表 routing.yaml 是会调的（曾从 haiku 起步改成 sonnet
    # 起步），写死会让「改表」和「改熔断」这两件不相干的事互相打架。
    # 这里要验的是「按阶梯前两档走、且没走到第三档」。
    from factory.audit.models import OracleClass
    from factory.routing import Router
    ladder = Router.default()  # _task() 有 checks + declared_paths → A 类
    assert [m for _, m in adapter.calls] == [
        ladder.model_for(OracleClass.A, n) for n in (1, 2)
    ]
    assert "连续 2 次挂死" in report.escalation_reason
    assert "执行环境故障" in report.escalation_reason


def test_single_stall_still_retries(store, tmp_path, spec_doc):  # noqa: F811
    """单次挂死不熔断 —— 可能只是中转站抖一下，值得再试。

    实测那次派发里，唯一跑通的产出正是重试拿到的。阈值设成 1 会把
    这种可恢复的抖动也判死。
    """
    spec_doc(tmp_path)
    adapter = FakeAdapter([_stalled(), _result()])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.MERGED
    assert report.rounds == 2


def test_stall_streak_resets_on_productive_round(
        store, tmp_path, spec_doc):  # noqa: F811
    """挂死→正常→挂死 不算连续，不该熔断。

    连续性才是环境故障的信号。按累计次数判会把「偶发抖动多次」误判成
    「环境坏了」，而那两种该有不同的处置。
    """
    spec_doc(tmp_path)
    adapter = FakeAdapter([_stalled(), _result(), _stalled()])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    # 第二轮全绿就落地了，压根走不到第三轮 —— 这里断言的是「没有因为
    # 第一轮的挂死留下计数残留而提前熔断」。
    assert report.outcome == Outcome.MERGED
    assert report.rounds == 2
