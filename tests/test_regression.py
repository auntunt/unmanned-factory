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


def test_exit_zero_captures_both_stdout_and_stderr(ws):
    """stderr 有噪声警告、stdout 有真失败摘要时，got 必须同时包含两路。

    这是 regression claim 诊断消失的根因：`proc.stderr or proc.stdout`
    只要 stderr 非空就完全不看 stdout，而 pytest 把失败摘要写在 stdout。
    """
    # 模拟 pytest 场景：stderr 打一行 uv 警告，stdout 打真正的失败摘要
    cmd = (
        'echo "warning: VIRTUAL_ENV does not match" >&2; '
        'echo "FAILED test_foo.py::test_bar - assert 1 == 2"; '
        'exit 1'
    )
    claim = run_check(CheckSpec("mixed", cmd), ws)
    assert claim is not None
    assert claim["check"] == "mixed"
    # got 里必须能看到 stdout 的失败摘要，不能被 stderr 噪声挤掉
    assert "FAILED" in claim["got"]
    assert "assert 1 == 2" in claim["got"]
    # 同时 stderr 的警告也在（为了完整性）
    assert "VIRTUAL_ENV" in claim["got"]
    # 标清了来源
    assert "[stderr]" in claim["got"]
    assert "[stdout]" in claim["got"]


def test_exit_zero_preserves_tail_for_pytest_summary(ws):
    """失败摘要在尾部时，截断要优先保留尾部，不能掐头去尾。

    pytest 的 FAILED / assert 行在末尾，头部截断会把它切掉。
    """
    # 构造一个长输出：头部是噪声，尾部是失败摘要
    head_noise = "x" * 3000
    tail_summary = "FAILED test.py - AssertionError: expected 42"
    cmd = f'echo "{head_noise}"; echo "{tail_summary}"; exit 1'
    claim = run_check(CheckSpec("long", cmd), ws)
    assert claim is not None
    # 核心要求：尾部的失败摘要必须保留
    assert "FAILED test.py" in claim["got"]
    assert "AssertionError" in claim["got"]


def test_stdout_contains_captures_stderr_context(ws):
    """stdout_contains 失败时，stderr 的上下文也要进 got。"""
    cmd = 'echo "some error context" >&2; echo "actual output"'
    spec = CheckSpec("missing", cmd, expect="stdout_contains", value="expected text")
    claim = run_check(spec, ws)
    assert claim is not None
    # got 里应该同时有 stdout 和 stderr
    assert "actual output" in claim["got"]
    assert "error context" in claim["got"]


def test_commands_agree_shows_both_outputs_with_stderr(ws):
    """commands_agree 不匹配时，两边的 stderr 也要显示。"""
    cmd1 = 'echo "warn1" >&2; echo "out1"'
    cmd2 = 'echo "warn2" >&2; echo "out2"'
    spec = CheckSpec("mismatch", cmd1, expect="commands_agree", value=cmd2)
    claim = run_check(spec, ws)
    assert claim is not None
    # expected 和 got 都应该包含各自的 stdout 和 stderr
    assert "out1" in claim["got"]
    assert "out2" in claim["expected"]
    # stderr 警告也在
    assert "warn1" in claim["got"]
    assert "warn2" in claim["expected"]

