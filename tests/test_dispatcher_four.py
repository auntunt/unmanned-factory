"""四监工接进 dispatcher。spec §4.1 的扇出在这一层变成实际行为。

最要紧的一条：架构监工出的是**意见**，不能一票否决合并。
它若能否决，一个爱挑刺的监工就能把每个任务拖到三轮上人，
P1 判据「上人平均打回次数 ≤ 1」当场作废。
"""
import pytest

from factory.audit.models import (
    OracleClass, Resolution, SupervisorRole, Verdict,
)
from factory.audit.store import AuditStore
from factory.dispatcher import Dispatcher, Outcome
from factory.harness.base import AttemptResult, ExitStatus
from factory.supervisors.base import SupervisorReport
from factory.task import CheckSpec, Task

from tests.test_dispatcher import AlwaysPass, FakeAdapter, _result


class StubSupervisor:
    """按 role 出预设裁决，并记录被调了几次。"""

    def __init__(self, role, verdict, claims=(), cost=0.02):
        self.role = role
        self._verdict = verdict
        self._claims = tuple(claims)
        self._cost = cost
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return SupervisorReport(role=self.role, verdict=self._verdict,
                                claims=self._claims, tokens=300,
                                cost_usd=self._cost)


def _claim(check="AC-1"):
    return {"check": check, "expected": "str", "got": "int"}


def _task(**kw):
    # 原来是 `spec_ref=("AC-1: 必须返回 str",)` —— 把正文塞进编号字段绕过了
    # 「编号没正文」的问题，而那恰好是 spec_doc 要修的形状。现在编号归编号、
    # 正文归文档（conftest 的 _prd_for_shared_task 写的），解析出来的字面
    # 仍是 `AC-1: 必须返回 str`。
    base = dict(
        task_id="T-4", prompt="加个函数",
        spec_ref=("AC-1",), spec_doc="prd.md",
        declared_paths=("greet.py",),
        checks=(CheckSpec(name="ok", command="true"),),
    )
    base.update(kw)
    return Task(**base)


def _build(tmp_path, *, spec=None, arch=None, results=None):
    store = AuditStore(tmp_path / "a.db")
    adapter = FakeAdapter(results or [_result()])
    d = Dispatcher(adapter=adapter, store=store, supervisor=AlwaysPass(),
                   spec_supervisor=spec, architecture_supervisor=arch)
    return d, store, adapter


def test_model_supervisors_are_off_by_default(tmp_path):
    """默认不调模型监工：P0 已证明零成本能跑通 A 类，默认开启会让最便宜的路径变贵。"""
    d, store, _ = _build(tmp_path)
    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.MERGED
    roles = {v.role for v in store.get(rep.attempt_ids[0]).supervisors}
    # scope 也在里面：它确定性、零成本，所以和 regression / risk 一样默认开着。
    # 「默认关」针对的是花钱的监工。
    assert roles == {SupervisorRole.REGRESSION, SupervisorRole.RISK,
                     SupervisorRole.SCOPE}
    costs = {v.role: v.cost_usd for v in store.get(rep.attempt_ids[0]).supervisors}
    assert costs[SupervisorRole.SCOPE] == 0.0


def test_all_four_verdicts_land_in_the_audit_row(tmp_path):
    """spec §5：四个监工各自一条记录，两周后才算得出各自的命中率。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.PASS)
    arch = StubSupervisor(SupervisorRole.ARCHITECTURE, Verdict.PASS)
    d, store, _ = _build(tmp_path, spec=spec, arch=arch)

    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.MERGED
    row = store.get(rep.attempt_ids[0])
    assert {v.role for v in row.supervisors} == {
        SupervisorRole.REGRESSION, SupervisorRole.RISK,
        SupervisorRole.SPEC, SupervisorRole.ARCHITECTURE,
        SupervisorRole.SCOPE,
    }
    costs = {v.role: v.cost_usd for v in row.supervisors}
    assert costs[SupervisorRole.SPEC] == 0.02
    assert costs[SupervisorRole.REGRESSION] == 0.0   # 确定性监工仍然零成本


def test_spec_supervisor_can_block_a_merge(tmp_path):
    """规格监工产的是证据（对着标准核 diff），所以它是硬的。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.FAIL, [_claim()])
    d, store, adapter = _build(tmp_path, spec=spec)

    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED
    assert rep.rounds == 3
    assert "AC-1" in adapter.calls[1][0]     # 具体失败项带回给了 worker


def test_architecture_alone_never_blocks_the_final_round(tmp_path):
    """只有架构监工报红时，最后一轮照样合并。

    它出意见不出证据，没有客观裁判能证明它对。给意见一票否决权，
    等于让它单独把任务拖到上人 —— 那 P1 判据就永远不达标。
    """
    arch = StubSupervisor(SupervisorRole.ARCHITECTURE, Verdict.FAIL,
                          [_claim("重复实现 factory/text.py:slugify")])
    d, store, adapter = _build(tmp_path, arch=arch)

    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.MERGED
    assert rep.rounds == 3           # 前两轮当返工建议带回去了
    assert len(adapter.calls) == 3
    assert "重复实现" in adapter.calls[1][0]


def test_architecture_dissent_is_still_recorded_when_overruled(tmp_path):
    """被放行也要留证：不入库就没法在两周后判断它是在挑刺还是真发现了问题。"""
    arch = StubSupervisor(SupervisorRole.ARCHITECTURE, Verdict.FAIL, [_claim("死代码")])
    d, store, _ = _build(tmp_path, arch=arch)

    rep = d.run(_task(), tmp_path)
    last = store.get(rep.attempt_ids[-1])
    assert last.resolution == Resolution.MERGED
    dissent = [v for v in last.supervisors
               if v.role == SupervisorRole.ARCHITECTURE]
    assert dissent[0].verdict == Verdict.FAIL
    assert dissent[0].claims[0]["check"] == "死代码"


def test_architecture_plus_a_hard_red_still_escalates(tmp_path):
    """软只是「不能单独否决」，不是「可以忽略」：有硬红时该拦还是拦。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.FAIL, [_claim()])
    arch = StubSupervisor(SupervisorRole.ARCHITECTURE, Verdict.FAIL, [_claim("死代码")])
    d, store, adapter = _build(tmp_path, spec=spec, arch=arch)

    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED
    assert "AC-1" in rep.escalation_reason


def test_no_model_calls_when_the_harness_produced_nothing(tmp_path):
    """空 diff 时调模型监工是纯烧钱：没有 diff 可核。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.PASS)
    arch = StubSupervisor(SupervisorRole.ARCHITECTURE, Verdict.PASS)
    d, store, _ = _build(tmp_path, spec=spec, arch=arch,
                         results=[_result(paths=())])

    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED
    assert spec.calls == 0
    assert arch.calls == 0


def test_no_model_calls_when_hard_gate_blocks_pre_dispatch(tmp_path):
    """D 类连 harness 都不调，更不该调监工。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.PASS)
    d, store, adapter = _build(tmp_path, spec=spec)

    rep = d.run(_task(declared_ops=("prod_deploy",)), tmp_path)
    assert rep.outcome == Outcome.BLOCKED_HARD_GATE
    assert spec.calls == 0
    assert adapter.calls == []


def test_spec_supervisor_receives_criteria_and_diff_but_not_the_log(tmp_path):
    """§4.1 的输入表在 dispatcher 这一层也要成立，不只在监工内部。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.PASS)
    d, _, _ = _build(tmp_path, spec=spec)
    d.run(_task(), tmp_path)

    assert spec.kwargs["criteria"] == ("AC-1: 必须返回 str",)
    assert "def greet" in spec.kwargs["diff"]
    assert "withheld" in spec.kwargs


def test_the_supervisor_gets_the_body_not_the_bare_ref(tmp_path, spec_doc):
    """监工手上必须是正文。断言 dispatcher 的**接线**，不是 criteria() 的返回值。

    单测 `task.criteria()` 挡不住这条：把 dispatcher 的
    `criteria=task.criteria(workspace)` 换回 `criteria=task.spec_ref`，
    那个单测照样全绿，而监工又拿回了四个字符的编号。所以断言从监工
    **实际收到的 kwargs** 上取。
    """
    spec_doc(tmp_path, {"AC-7": "错误路径必须带 request_id"})
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.PASS)
    d, _, _ = _build(tmp_path, spec=spec)
    d.run(_task(spec_ref=("AC-7",), acceptance=("另外一条",)), tmp_path)

    got = spec.kwargs["criteria"]
    assert got == ("AC-7: 错误路径必须带 request_id", "另外一条")
    assert "AC-7" not in got, "裸编号不能作为一条标准递过去"


# ---------- 第三类：监工自己坏了 ----------

def _fault(role):
    """监工故障的裁决形状，和 report_from_call 失败分支一致。"""
    return StubSupervisor(role, Verdict.FAIL, [{
        "check": f"supervisor-{role.value}-unavailable",
        "command": "",
        "expected": "给出 pass/fail 裁决",
        "got": "timeout after 300s",
    }])


def test_supervisor_outage_blocks_the_merge(tmp_path):
    """没审过 ≠ 没问题。监工挂了不能静默合并。"""
    d, store, _ = _build(tmp_path, spec=_fault(SupervisorRole.SPEC))
    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED


def test_supervisor_outage_does_not_burn_retries_on_the_worker(tmp_path):
    """worker 修不了监工的超时。打回给它就是白烧三轮 token。"""
    d, store, adapter = _build(tmp_path, spec=_fault(SupervisorRole.SPEC))
    rep = d.run(_task(), tmp_path)

    assert rep.rounds == 1
    assert len(adapter.calls) == 1
    assert "监工不可用" in rep.escalation_reason
    assert "timeout" in rep.escalation_reason


def test_architecture_outage_also_escalates_even_though_it_is_soft(tmp_path):
    """软的是它的**意见**，不是它的故障。故障说明这一轮没审完。"""
    d, store, _ = _build(tmp_path, arch=_fault(SupervisorRole.ARCHITECTURE))
    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED
    assert rep.rounds == 1


def test_real_claims_still_go_to_the_worker_when_mixed_with_a_fault(tmp_path):
    """混在一起时，故障优先：先把监工修好，再谈返工。"""
    spec = StubSupervisor(SupervisorRole.SPEC, Verdict.FAIL, [_claim()])
    d, store, adapter = _build(tmp_path, spec=spec,
                               arch=_fault(SupervisorRole.ARCHITECTURE))
    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED
    assert rep.rounds == 1
    assert "监工不可用" in rep.escalation_reason
