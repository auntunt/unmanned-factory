"""闸门与探针的可观测性（§9.1 / §9.2 回归测试）。

§9.1: ProbeResult.skipped 从不被读 —— 非标测试目录（spec/、t/）的仓库
      落在盲区里，伪造的绿能出货，而报表上看不出任何异常。
§9.2: 13 道闸门全报 role='risk'，按 role 聚合把 13 件事的命中率搅成一个数；
      一道从不触发的闸门（判据写坏了）在报表上完全隐身。
"""

import ast
from pathlib import Path

import pytest

from factory.audit.models import Resolution, SupervisorRole, Verdict
from factory.gate_claims import FAULT_CLAIMS, GATE_CLAIMS
from factory.dispatcher import Dispatcher
from factory.metrics import GateMetrics, gate_metrics
from factory.supervisors.base import SupervisorReport

REPO = Path(__file__).resolve().parent.parent


# ---------- §9.1: BEACON 让探针盲区可见 ----------


def test_beacon_role_exists():
    """新增的 role 必须能存进 String(16) 的列。"""
    assert SupervisorRole.BEACON == "beacon"
    assert len(SupervisorRole.BEACON.value) <= 16


def test_beacon_never_blocks_the_merge():
    """BEACON 是可见性记录，不是闸门 —— 它的 claims 不许拦合并。

    拦住的后果：所有测试目录不叫 tests/ 的仓库一律无法出货。那是把
    「审计里看不见盲区」换成「整类仓库停摆」，换亏了。
    """
    beacon = SupervisorReport(
        role=SupervisorRole.BEACON,
        verdict=Verdict.PASS,
        claims=({"check": "probe-skipped", "command": "pytest -q",
                 "actual": "没有带测试的目录"},),
    )
    regression = SupervisorReport(role=SupervisorRole.REGRESSION,
                                  verdict=Verdict.PASS)

    merged = Dispatcher._merge_reports((regression, beacon), is_last=False)
    assert not merged.blocking, f"BEACON 拦住了合并：{merged.blocking}"


def test_beacon_claims_do_not_leak_into_soft_or_hard():
    """连软意见都不算 —— 它不是意见，是事实记录。"""
    beacon = SupervisorReport(
        role=SupervisorRole.BEACON,
        verdict=Verdict.PASS,
        claims=({"check": "probe-skipped", "actual": "探针自己超时"},),
    )
    merged = Dispatcher._merge_reports((beacon,), is_last=True)
    assert merged.blocking is False
    # feedback 会打回 worker，faults 会升级给人 —— 两条路都不该有它。
    # worker 修不了「这个仓库的测试目录不叫 tests/」，人也不需要为此被叫起来。
    for bucket_name in ("feedback", "faults"):
        bucket = getattr(merged, bucket_name, ())
        assert not any(
            c.get("check") == "probe-skipped" for c in bucket
        ), f"probe-skipped 混进了 {bucket_name}"


def test_probe_skip_reasons_are_all_reachable():
    """verdict_probe 的 4 个 skip 出口都返回 skipped=True。

    这一条防的是有人给某个出口补了 skipped=False 却忘了它意味着「验过了」。
    """
    from factory.harness.verdict_probe import ProbeResult, probe

    # 默认值就是 skipped=True —— 4 个 skip 出口都走 ProbeResult(reason=...)
    assert ProbeResult().skipped is True
    assert ProbeResult().fake_green is False

    # 出口 1: 没有测试目录
    empty = Path("/tmp/probe_no_tests_dir_xyz")
    empty.mkdir(exist_ok=True)
    res = probe(empty, "pytest -q")
    assert res.skipped is True and res.fake_green is False

    # 出口 2: 不是 pytest 命令
    res = probe(REPO, "npm test")
    assert res.skipped is True, "非 pytest 命令必须 skip 而不是判绿"


# ---------- §9.2: 闸门粒度聚合 ----------


class _FakeVerdict:
    def __init__(self, role, verdict, claims, cost_usd=0.0, tokens=0):
        self.role, self.verdict, self.claims = role, verdict, claims
        self.cost_usd, self.tokens = cost_usd, tokens


class _FakeAttempt:
    def __init__(self, resolution, supervisors, linked_defects=()):
        self.resolution = resolution
        self.supervisors = supervisors
        self.linked_defects = list(linked_defects)


class _FakeStore:
    def __init__(self, attempts):
        self._attempts = attempts

    def all_attempts(self, *, task_id=None):
        return self._attempts


def test_gate_metrics_separates_gates_sharing_one_role():
    """两道闸门都是 role='risk'，命中率必须分开算。

    这是 §9.2 的核心：按 role 聚合时 fake-green 的 2/2 和 shadow-code 的
    0/2 会被平均成 50%，两道闸门都看不出真实表现。
    """
    store = _FakeStore([
        # fake-green 报了 2 次，都是真的
        _FakeAttempt(Resolution.REWORKED, [
            _FakeVerdict("risk", Verdict.FAIL, [{"check": "fake-green"}])]),
        _FakeAttempt(Resolution.REWORKED, [
            _FakeVerdict("risk", Verdict.FAIL, [{"check": "fake-green"}])]),
        # shadow-code 报了 2 次，都是误报
        _FakeAttempt(Resolution.MERGED, [
            _FakeVerdict("risk", Verdict.FAIL, [{"check": "shadow-code"}])]),
        _FakeAttempt(Resolution.MERGED, [
            _FakeVerdict("risk", Verdict.FAIL, [{"check": "shadow-code"}])]),
    ])

    m = gate_metrics(store)
    assert m["risk:fake-green"].hit_rate == 1.0
    assert m["risk:shadow-code"].hit_rate == 0.0
    assert m["risk:fake-green"].verdict_line() == "保留"
    assert "误报" in m["risk:shadow-code"].verdict_line()


def test_never_fired_gates_still_appear():
    """从不触发的闸门必须在报表上占一行 —— 这是这个函数存在的理由。"""
    store = _FakeStore([
        _FakeAttempt(Resolution.REWORKED, [
            _FakeVerdict("risk", Verdict.FAIL, [{"check": "fake-green"}])]),
    ])

    m = gate_metrics(store, known_gates=GATE_CLAIMS)
    silent = m["risk:head-moved"]
    assert silent.fired == 0
    assert "从未触发" in silent.verdict_line()
    # 13 道闸门一道都不能少
    for gate in GATE_CLAIMS:
        assert f"risk:{gate}" in m, f"{gate} 从报表上消失了"


def test_gate_metrics_ignores_passing_verdicts():
    """PASS 的裁决没有 claims，不该凭空造出闸门条目。"""
    store = _FakeStore([
        _FakeAttempt(Resolution.MERGED, [_FakeVerdict("risk", Verdict.PASS, [])]),
    ])
    assert gate_metrics(store) == {}


def test_multi_claim_verdict_counts_each_gate_once():
    """一次裁决带多条 claim 时，每道闸门各记一次。"""
    store = _FakeStore([
        _FakeAttempt(Resolution.REWORKED, [
            _FakeVerdict("risk", Verdict.FAIL, [
                {"check": "head-moved"}, {"check": "git-config-touched"}])]),
    ])
    m = gate_metrics(store)
    assert m["risk:head-moved"].fired == 1
    assert m["risk:git-config-touched"].fired == 1


def test_dashboard_claim_tables_cover_every_blocked_name():
    """不变量：dispatcher 和 gates 里每个闸门名都要在两张表之一里。

    漏一个的后果是那道闸门在页面上没有说明文字、在闸门报表上不存在 ——
    它拦了货但没人知道是什么拦的。用 AST 抓而不是正则：正则会被
    多行调用和注释里的字符串骗到。

    十道 git 闸门搬到 gates/ 之后,dispatcher 里只剩 `self._blocked(breach.name)`
    一个间接调用,真正的名字在 `GateSpec.name` 字段里。所以两个文件都要扫。
    """
    names: set[str] = set()

    # 1. dispatcher.py 里的直接调用: self._blocked("harness", ...)
    disp_src = (REPO / "factory" / "dispatcher.py").read_text(encoding="utf-8")
    disp_tree = ast.parse(disp_src)
    for node in ast.walk(disp_tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # self._blocked("gate-name", ...)
        if isinstance(fn, ast.Attribute) and fn.attr == "_blocked":
            if node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)

    # 2. gates/specs.py 里的 GateSpec(name="...", ...)
    gates_src = (REPO / "factory" / "gates" / "specs.py").read_text(encoding="utf-8")
    gates_tree = ast.parse(gates_src)
    for node in ast.walk(gates_tree):
        if not isinstance(node, ast.Call):
            continue
        # GateSpec(name="git-hook-touched", ...)
        if isinstance(node.func, ast.Name) and node.func.id == "GateSpec":
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        names.add(kw.value.value)

    assert names, "一个闸门名都没抓到，AST 遍历写坏了"

    documented = set(GATE_CLAIMS) | set(FAULT_CLAIMS)
    undocumented = names - documented
    assert not undocumented, (
        f"这些闸门拦了货却没有说明文字：{sorted(undocumented)}。"
        f"加进 gate_claims.GATE_CLAIMS（是闸门）或 FAULT_CLAIMS（是故障）"
    )

    stale = documented - names
    assert not stale, (
        f"这些名字在 gate_claims 表里但代码已经不用了：{sorted(stale)}"
    )
