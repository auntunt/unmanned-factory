import pytest

from factory.audit.models import SupervisorRole, Verdict
from factory.supervisors.regression import RegressionSupervisor, run_check
from factory.task import CheckSpec


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "greet.py").write_text("def greet(n):\n    return n\n",
                                       encoding="utf-8")
    return tmp_path


def test_exit_zero_pass(ws):
    assert run_check(CheckSpec("ok", "true"), ws) is None


def test_exit_zero_fail_records_command_and_code(ws):
    claim = run_check(CheckSpec("bad", "exit 3"), ws)
    assert claim["check"] == "bad"
    assert claim["expected"] == "exit_zero"
    assert "3" in claim["got"]


def test_stdout_contains_pass(ws):
    spec = CheckSpec("has-greet", "cat greet.py",
                     expect="stdout_contains", value="def greet")
    assert run_check(spec, ws) is None


def test_stdout_contains_fail(ws):
    spec = CheckSpec("has-farewell", "cat greet.py",
                     expect="stdout_contains", value="def farewell")
    claim = run_check(spec, ws)
    assert claim is not None
    assert "def farewell" in claim["expected"]


def test_commands_agree_pass(ws):
    spec = CheckSpec("same", "echo abc", expect="commands_agree",
                     value="echo abc")
    assert run_check(spec, ws) is None


def test_commands_agree_fail(ws):
    """部署 runbook 里的 image-id 核对：两条命令输出必须一致。"""
    spec = CheckSpec("image-id", "echo local-sha", expect="commands_agree",
                     value="echo running-sha")
    claim = run_check(spec, ws)
    assert claim is not None
    assert "local-sha" in claim["got"]
    assert "running-sha" in claim["expected"]


def test_runs_in_workspace_cwd(ws):
    spec = CheckSpec("cwd", "ls", expect="stdout_contains", value="greet.py")
    assert run_check(spec, ws) is None


def test_timeout_becomes_claim(ws):
    claim = run_check(CheckSpec("slow", "sleep 5", timeout_s=1), ws)
    assert claim is not None
    # "timeout" 这个词是承重的：漏账探测器按它认超时。
    assert "timeout" in claim["got"]


def test_a_timed_out_check_leaves_no_children_behind(ws, leak_probe):
    """check 命令是 task YAML 里用户写的 shell，它派生的东西 sh 自己不管。

    上面那条只看 claim 文本 —— 把 run_bounded 换回 subprocess.run，它照样
    过。真正要断言的是那个孙子进程死了：一条挂住的 check 留下的 pytest /
    node 会接着占机器。
    """
    probe = leak_probe("check")
    claim = run_check(CheckSpec("leaky", probe.command, timeout_s=1), ws)
    assert claim is not None and "timeout" in claim["got"]
    probe.assert_reaped()


def test_unknown_expect_becomes_claim(ws):
    claim = run_check(CheckSpec("weird", "true", expect="vibes"), ws)
    assert claim is not None
    assert "unknown expect" in claim["got"]


def test_review_all_pass(ws):
    report = RegressionSupervisor().review(ws, [
        CheckSpec("a", "true"),
        CheckSpec("b", "cat greet.py", expect="stdout_contains",
                  value="def greet"),
    ])
    assert report.role == SupervisorRole.REGRESSION
    assert report.verdict == Verdict.PASS
    assert report.passed is True
    assert report.claims == ()
    assert report.tokens == 0 and report.cost_usd == 0.0


def test_review_collects_every_failure_not_just_first(ws):
    report = RegressionSupervisor().review(ws, [
        CheckSpec("a", "exit 1"),
        CheckSpec("b", "true"),
        CheckSpec("c", "exit 2"),
    ])
    assert report.verdict == Verdict.FAIL
    assert [c["check"] for c in report.claims] == ["a", "c"]


def test_review_with_no_checks_fails(ws):
    """没有裁判 ≠ 通过。A 类的前提就是廉价客观裁判存在。"""
    report = RegressionSupervisor().review(ws, [])
    assert report.verdict == Verdict.FAIL
    assert report.claims[0]["check"] == "no-checks-defined"
