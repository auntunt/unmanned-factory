from dataclasses import replace

import pytest

from factory.audit.models import (
    OracleClass, Resolution, SupervisorRole, Verdict,
)
from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.grading.rules import GradingEngine
from factory.harness.base import AttemptResult, ExitStatus, Limits
from factory.supervisors.base import SupervisorReport
from factory.task import CheckSpec, Task


class FakeAdapter:
    """按脚本返回预设结果，并记录每轮收到的 prompt 和 model。"""

    name = "fake"

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, str | None]] = []

    def run(self, task, workspace, limits, *, model=None):
        self.calls.append((task.prompt, model))
        return self._results[min(len(self.calls) - 1, len(self._results) - 1)]


def _result(paths=("greet.py",), status=ExitStatus.OK):
    return AttemptResult(
        exit_status=status,
        diff="--- a/greet.py\n+++ b/greet.py\n+def greet(n): return n\n",
        diff_hash="a" * 64,
        changed_paths=tuple(paths),
        transcript_path="/tmp/s.jsonl",
        tokens_in=10, tokens_out=20, cost_usd=0.001, wall_clock_ms=1500,
        session_id="sess-1", harness_version="2.1.223",
    )


class AlwaysPass:
    role = SupervisorRole.REGRESSION

    def review(self, workspace, checks):
        return SupervisorReport(role=self.role, verdict=Verdict.PASS)


class FailsThenPasses:
    role = SupervisorRole.REGRESSION

    def __init__(self, fail_times: int):
        self._left = fail_times
        self.seen = 0

    def review(self, workspace, checks):
        self.seen += 1
        if self._left > 0:
            self._left -= 1
            return SupervisorReport(
                role=self.role, verdict=Verdict.FAIL,
                claims=({"check": "pytest", "expected": "exit_zero",
                         "got": "exit 1: 2 failed"},),
            )
        return SupervisorReport(role=self.role, verdict=Verdict.PASS)


@pytest.fixture
def store():
    return AuditStore(":memory:")


def _task(**kw):
    base = dict(task_id="T-1", prompt="add greet", spec_ref=("AC-1",),
                declared_paths=("greet.py",),
                checks=(CheckSpec("pytest", "true"),))
    return Task(**{**base, **kw})


def _dispatcher(store, adapter, supervisor=None, engine=None):
    return Dispatcher(
        adapter=adapter, store=store,
        engine=engine or GradingEngine.default(),
        supervisor=supervisor or AlwaysPass(),
        limits=Limits(max_turns=5, timeout_s=60),
    )


def _verdict(row, role):
    """取某个角色的裁决。每轮都会落两条：role=risk 的后分级 + 回归监工。"""
    matches = [v for v in row.supervisors if v.role == role]
    assert matches, f"没有 role={role} 的裁决"
    return matches[-1]


def test_class_a_all_green_merges_on_first_round(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.MERGED
    assert report.rounds == 1
    assert len(report.attempt_ids) == 1

    row = store.get(report.attempt_ids[0])
    assert row.resolution == Resolution.MERGED
    assert row.oracle_class == OracleClass.A
    assert row.spec_ref == ["AC-1"]
    assert row.harness == "fake"
    assert row.harness_version == "2.1.223"
    assert row.model == "haiku"
    assert row.diff_hash == "a" * 64
    assert row.transcript_path == "/tmp/s.jsonl"
    assert (row.tokens_in, row.tokens_out) == (10, 20)
    assert row.wall_clock_ms == 1500
    # 一轮落两条裁决：后分级（risk，未升级 → PASS）+ 回归监工
    assert _verdict(row, SupervisorRole.RISK).verdict == Verdict.PASS
    assert _verdict(row, SupervisorRole.REGRESSION).verdict == Verdict.PASS


def test_failure_sends_claims_back_and_escalates_model(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, FailsThenPasses(1)).run(
        _task(), tmp_path
    )

    assert report.outcome == Outcome.MERGED
    assert report.rounds == 2
    # 第二轮的 prompt 必须带上具体失败项
    assert "exit 1: 2 failed" in adapter.calls[1][0]
    # 第二轮换更强的模型
    assert [m for _, m in adapter.calls] == ["haiku", "sonnet"]
    assert store.get(report.attempt_ids[0]).resolution == Resolution.REWORKED
    assert store.get(report.attempt_ids[1]).resolution == Resolution.MERGED


def test_first_round_prompt_is_untouched(store, tmp_path):
    adapter = FakeAdapter([_result()])
    _dispatcher(store, adapter).run(_task(), tmp_path)
    assert adapter.calls[0][0] == "add greet"


def test_three_strikes_escalates_to_human(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, FailsThenPasses(99)).run(
        _task(), tmp_path
    )

    assert report.outcome == Outcome.ESCALATED
    assert report.rounds == 3
    assert len(report.attempt_ids) == 3
    # 前两轮 reworked、最后一轮 escalated —— 两周后按 resolution 算命中率时，
    # 中间轮次不能也记成 escalated，否则返工与升级混成一类
    assert [store.get(i).resolution for i in report.attempt_ids] == [
        Resolution.REWORKED, Resolution.REWORKED, Resolution.ESCALATED,
    ]
    assert "3 轮未通过" in report.escalation_reason


def test_pre_dispatch_class_c_never_runs_the_agent(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter).run(
        _task(declared_paths=("src/auth/login.py",)), tmp_path
    )

    assert report.outcome == Outcome.ESCALATED
    assert adapter.calls == []          # 一次都没派发
    assert report.final_grade.oracle_class == OracleClass.C
    row = store.get(report.attempt_ids[0])
    assert row.resolution == Resolution.ESCALATED
    assert "C:" in row.class_reason


def test_pre_dispatch_class_d_is_hard_gate(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter).run(
        _task(declared_ops=("prod_deploy",)), tmp_path
    )

    assert report.outcome == Outcome.BLOCKED_HARD_GATE
    assert adapter.calls == []
    assert store.get(report.attempt_ids[0]).oracle_class == OracleClass.D
    assert "硬闸门" in report.escalation_reason


def test_post_diff_escalation_when_agent_touched_worse_paths(store, tmp_path):
    """预分级 A，实际改了 migrations/ → 必须升级，且不能 merge。"""
    adapter = FakeAdapter([_result(paths=("greet.py", "db/migrations/007.py"))])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.BLOCKED_HARD_GATE
    assert report.final_grade.oracle_class == OracleClass.D
    row = store.get(report.attempt_ids[0])
    assert row.oracle_class == OracleClass.D
    assert "db/migrations/007.py" in row.class_reason
    assert row.resolution == Resolution.ESCALATED
    # 后分级独立成一条 risk 记录，便于两周后算命中率
    assert _verdict(row, SupervisorRole.RISK).verdict == Verdict.FAIL
    # 升级后不许再跑回归监工 —— 这一轮不该有回归裁决
    assert not [v for v in row.supervisors
                if v.role == SupervisorRole.REGRESSION]


def test_post_diff_same_severity_does_not_escalate(store, tmp_path):
    # 两个文件都得声明：这条测的是后分级的严重度比较，不是范围。
    # 只声明 greet.py 会被范围监工拦下，测试就变成在测另一件事了。
    adapter = FakeAdapter([_result(paths=("greet.py", "utils.py"))])
    task = _task(declared_paths=("greet.py", "utils.py"))
    report = _dispatcher(store, adapter).run(task, tmp_path)
    assert report.outcome == Outcome.MERGED
    assert report.final_grade.oracle_class == OracleClass.A


def test_harness_error_counts_as_a_failed_round(store, tmp_path):
    adapter = FakeAdapter([replace(_result(), exit_status=ExitStatus.ERROR,
                                   error_text="error_max_turns")])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)

    assert report.outcome == Outcome.ESCALATED
    assert report.rounds == 3
    claims = _verdict(store.get(report.attempt_ids[0]),
                      SupervisorRole.REGRESSION).claims
    assert any("error_max_turns" in str(c) for c in claims)


def test_empty_diff_is_a_failed_round(store, tmp_path):
    adapter = FakeAdapter([replace(_result(), diff="", diff_hash=None,
                                   changed_paths=())])
    report = _dispatcher(store, adapter).run(_task(), tmp_path)
    assert report.outcome == Outcome.ESCALATED
    claims = _verdict(store.get(report.attempt_ids[0]),
                      SupervisorRole.REGRESSION).claims
    assert any("no changes" in str(c) for c in claims)


def test_max_rounds_from_task_is_respected(store, tmp_path):
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, FailsThenPasses(99)).run(
        _task(max_rounds=1), tmp_path
    )
    assert report.rounds == 1
    assert report.outcome == Outcome.ESCALATED


def test_real_regression_supervisor_end_to_end_offline(store, tmp_path):
    """不塞假监工，用真的回归监工跑一遍 —— 确认接线正确。"""
    adapter = FakeAdapter([_result()])
    d = Dispatcher(adapter=adapter, store=store,
                   engine=GradingEngine.default(), limits=Limits())
    report = d.run(_task(checks=(CheckSpec("ok", "true"),)), tmp_path)
    assert report.outcome == Outcome.MERGED


def test_task_without_checks_never_merges(store, tmp_path):
    """没有廉价裁判就不是 A 类。真监工判 FAIL，三轮后升级给人。"""
    adapter = FakeAdapter([_result()])
    d = Dispatcher(adapter=adapter, store=store,
                   engine=GradingEngine.default(), limits=Limits())
    report = d.run(_task(checks=()), tmp_path)
    assert report.outcome == Outcome.ESCALATED
    claims = _verdict(store.get(report.attempt_ids[0]),
                      SupervisorRole.REGRESSION).claims
    assert claims[0]["check"] == "no-checks-defined"


def test_a_merge_that_took_two_rounds_leaves_a_complete_audit_trail(store, tmp_path):
    """多轮 merge 的审计记录 —— 这些是 e2e smoke 判据依赖的不变式。

    smoke 测试原来写死 `get(1)` 和 `model == "haiku"`，等于在断言「模型一次
    就写对」。真的用了两轮时它会挂，而那是一次完全正常的 merge —— 于是
    「真的坏了」和「模型这次多用了一轮」在夜跑里分不开。

    smoke 改成取最后一轮之后，多轮这条路就只有靠模型偶尔失手才被走到。
    这个测试用 FailsThenPasses 把它钉成确定性的：不打模型、几毫秒跑完。
    """
    adapter = FakeAdapter([_result()])
    report = _dispatcher(store, adapter, supervisor=FailsThenPasses(1)).run(
        _task(max_rounds=2), tmp_path)

    assert report.outcome is Outcome.MERGED
    assert report.rounds == 2

    rows = store.attempts_for("T-1")
    assert [r.attempt_no for r in rows] == [1, 2]

    # 最后一轮：merged。前面每一轮：reworked 而不是 pending 或 escalated。
    assert rows[-1].resolution == Resolution.MERGED
    assert rows[0].resolution == Resolution.REWORKED

    # A 类阶梯 [haiku, sonnet, opus]：第几轮就该是第几档。
    assert [m for _, m in adapter.calls] == ["haiku", "sonnet"]
    assert [r.model for r in rows] == ["haiku", "sonnet"]

    # 打回的那一轮同样是审计现场：字段要齐，而且要能解释「为什么重跑」。
    assert rows[0].diff_hash and len(rows[0].diff_hash) == 64
    assert rows[0].cost_usd > 0
    assert any(v.verdict == Verdict.FAIL for v in rows[0].supervisors)
