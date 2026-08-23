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
                spec_doc="prd.md", declared_paths=("greet.py",),
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
    assert row.model == "sonnet"
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
    assert [m for _, m in adapter.calls] == ["sonnet", "opus"]
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

    # A 类阶梯 [sonnet, opus, opus]：第几轮就该是第几档。
    assert [m for _, m in adapter.calls] == ["sonnet", "opus"]
    assert [r.model for r in rows] == ["sonnet", "opus"]

    # 打回的那一轮同样是审计现场：字段要齐，而且要能解释「为什么重跑」。
    assert rows[0].diff_hash and len(rows[0].diff_hash) == 64
    assert rows[0].cost_usd > 0
    assert any(v.verdict == Verdict.FAIL for v in rows[0].supervisors)


# ---------- 落地：spec §5 的 commit 字段 ----------

def _wt(tmp_path, spec_doc=None):
    """一棵真的 linked worktree。land() 拒绝在主工作树提交，所以假不了。

    PRD 提交进 base：这批测试的 workspace 是 worktree 而不是 tmp_path，
    模块级那个 autouse 夹具写的 prd.md 在这里查不到。而且它必须**在 base 里**
    —— 留成未跟踪文件的话，land() 的 `git add` 会把 PRD 一起提交进去。
    """
    import subprocess

    def g(root, *a):
        return subprocess.run(["git", *a], cwd=root, capture_output=True,
                              text=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q")
    g(repo, "config", "user.email", "t@example.com")
    g(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("b\n")
    if spec_doc is not None:
        spec_doc(repo, {"AC-1": "greet 必须返回 str"})
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "base")
    wt = tmp_path / "wt"
    g(repo, "worktree", "add", "-q", "-b", "factory/t-1", str(wt), "HEAD")
    return repo, wt, g


def test_a_merged_attempt_records_the_commit_sha(store, tmp_path, spec_doc):
    """判绿 → 提交 → sha 落审计库。

    spec §5 把 commit 列为承重字段，而它一直写死 None，于是 spec 写明的
    漏报回查路径（git blame → commit → task_id）没有数据可走。
    """
    repo, wt, g = _wt(tmp_path, spec_doc)
    (wt / "greet.py").write_text("def greet(n): return n\n")

    report = _dispatcher(store, FakeAdapter([_result()])).run(_task(), wt)

    assert report.outcome is Outcome.MERGED
    assert report.commit and len(report.commit) == 40
    assert store.attempts_for("T-1")[-1].commit == report.commit
    # 提交在任务分支上，主分支没动
    assert g(wt, "rev-parse", "HEAD").stdout.strip() == report.commit
    assert g(repo, "status", "--porcelain").stdout.strip() == ""


def test_reworked_rounds_are_not_committed(store, tmp_path, spec_doc):
    """只有合并的那一轮提交，中途打回的不提交。

    硬理由不是「打回的产出没人查」，而是 capture_diff 用 `git diff HEAD`：
    中途提交会让下一轮的 diff 变成「相对上一轮的增量」而不是「这个任务改了
    什么」—— 审计里 diff_hash 的含义会在多轮任务上悄悄换掉。
    """
    repo, wt, g = _wt(tmp_path, spec_doc)
    (wt / "greet.py").write_text("def greet(n): return n\n")
    base = g(wt, "rev-parse", "HEAD").stdout.strip()

    report = _dispatcher(store, FakeAdapter([_result()]),
                         supervisor=FailsThenPasses(1)).run(
        _task(max_rounds=2), wt)

    assert report.rounds == 2
    rows = store.attempts_for("T-1")
    assert rows[0].resolution == Resolution.REWORKED
    assert rows[0].commit is None, "打回的那一轮不该有 commit"
    # 断言最后一轮**确实落地了**，不能只写 rows[1].commit == report.commit ——
    # 两边都是 None 时那条也成立。而「每轮都提交」这个缺陷的症状恰恰是：
    # 第一轮把改动吃掉，合并那轮无改动可提交，两边一起变 None。
    assert report.commit is not None, report.landing_note
    assert rows[1].commit == report.commit
    # 整个任务只多出一个 commit，不是每轮一个
    log = g(wt, "log", "--format=%H", f"{base}..HEAD").stdout.split()
    assert log == [report.commit]


def test_a_failed_landing_does_not_change_the_verdict(store, tmp_path, spec_doc):
    """落地失败不把绿的判成红。

    退化后果只是 commit 仍为 None —— 也就是这个功能存在之前的状态。
    让一个已经全绿的任务因为 user.email 没配变成 escalated，
    是拿真问题换假问题。这里用主工作树触发拒绝（land 的第一条防线）。
    """
    repo = tmp_path / "plain"
    repo.mkdir()
    import subprocess
    for a in (["init", "-q"], ["config", "user.email", "t@e.com"],
              ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=repo, capture_output=True)
    (repo / "base.txt").write_text("b\n")
    spec_doc(repo, {"AC-1": "greet 必须返回 str"})
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "b"], cwd=repo,
                   capture_output=True)
    (repo / "greet.py").write_text("def greet(n): return n\n")

    report = _dispatcher(store, FakeAdapter([_result()])).run(_task(), repo)

    assert report.outcome is Outcome.MERGED, "落地失败不该改判决"
    assert report.commit is None
    assert "linked worktree" in report.landing_note
    assert store.attempts_for("T-1")[-1].resolution == Resolution.MERGED


def test_an_attempt_can_be_found_back_from_a_short_sha(store, tmp_path, spec_doc):
    """git blame 给的是短 sha，回查必须接受它。

    逼人手动补全 40 位等于给漏报统计加一道摩擦，而需要额外动作的度量
    等于没有度量。
    """
    _, wt, _ = _wt(tmp_path, spec_doc)
    (wt / "greet.py").write_text("def greet(n): return n\n")
    report = _dispatcher(store, FakeAdapter([_result()])).run(_task(), wt)

    row = store.attempt_by_commit(report.commit[:8])
    assert row is not None and row.task_id == "T-1"
    assert store.attempt_by_commit(report.commit).id == row.id
    assert store.attempt_by_commit("0" * 12) is None
    assert store.attempt_by_commit("") is None


def test_the_commit_covers_exactly_what_diff_hash_covered(store, tmp_path, spec_doc):
    """commit 和 diff_hash 必须描述同一组文件。

    真跑抓到的：check 命令跑 `python3 -c "from src.text import f"` 留下
    __pycache__/*.pyc，`add -A` 把它们一起提交了。diff_hash 只覆盖
    src/text.py，commit 里却多两个 .pyc —— 监工审的是前者，出货的是后者。

    钉的是 dispatcher 的接线（changed_paths 有没有真的传下去），
    不只是 land() 自己的行为。
    """
    _, wt, g = _wt(tmp_path, spec_doc)
    (wt / "greet.py").write_text("def greet(n): return n\n")
    cache = wt / "__pycache__"
    cache.mkdir()
    (cache / "greet.cpython-312.pyc").write_bytes(b"\x00junk")

    report = _dispatcher(store, FakeAdapter([_result(paths=("greet.py",))])
                         ).run(_task(), wt)

    assert report.commit, report.landing_note
    names = g(wt, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert names == ["greet.py"], f"提交了监工没审过的东西：{names}"


# ---------- .checks.json 契约的注入（断链回归） ----------


def test_checkless_task_is_told_how_to_leave_checks(store, tmp_path):
    """没有 checks 的任务，prompt 里必须带上 .checks.json 契约。

    这是一条断链的回归：读取端一直在等 worktree 根的 .checks.json，而
    worker 从没被告知这个文件的存在 —— 于是它必然三轮全红在
    no-checks-defined 上（audit.db 里两条实测轨迹，$4.96 / 900s 换回一句
    「你没写判据」）。契约不送达，这条无人路径就是确定烧钱。
    """
    from factory.checks_contract import CHECKS_FILENAME

    adapter = FakeAdapter([_result()])
    d = Dispatcher(adapter=adapter, store=store,
                   engine=GradingEngine.default(), limits=Limits())
    d.run(_task(checks=()), tmp_path)

    first_prompt = adapter.calls[0][0]
    assert "add greet" in first_prompt, "原任务描述不能被契约挤掉"
    assert CHECKS_FILENAME in first_prompt


def test_task_with_checks_is_not_told_to_write_its_own(store, tmp_path):
    """自带 checks 的任务不附契约。

    判据已经有了，再要 worker 写一份只会让它去猜一组和 YAML 里那组竞争的
    判据 —— 而 dispatcher 用的是 `task.checks or load(...)`，worker 写的那组
    根本不会被读，纯浪费 token 和轮次。
    """
    from factory.checks_contract import CHECKS_FILENAME

    adapter = FakeAdapter([_result()])
    _dispatcher(store, adapter).run(_task(), tmp_path)
    assert CHECKS_FILENAME not in adapter.calls[0][0]


def test_worker_written_checks_are_picked_up_and_can_merge(store, tmp_path):
    """worker 写下 .checks.json → 真回归监工读到它 → 能 merge。

    这条是「契约送达之后确实走得通」的端到端证据。上一条只证明我们说了，
    这条证明说了有用 —— 两条都要，缺后者的话读取端悄悄坏掉不会有人知道。
    """
    import json as _json
    import sys

    from factory.checks_contract import CHECKS_FILENAME

    class WritesChecks:
        """模拟一个照契约行事的 worker。"""

        name = "fake"

        def __init__(self):
            self.calls = []

        def run(self, task, workspace, limits, *, model=None):
            self.calls.append((task.prompt, model))
            # 真写出产物，再写一条真的验它的 check —— 用 `true` 当 check
            # 会被 vacuous-checks 闸门拦下（这一点本身有测试覆盖），
            # 那样这条测试就验不到「读取端接通了」这件事。
            (workspace / "greet.py").write_text(
                "def greet(n):\n    return n\n", encoding="utf-8")
            (workspace / CHECKS_FILENAME).write_text(
                _json.dumps({"checks": [
                    {"name": "unit",
                     "command": f"{sys.executable} -c 'import greet'"},
                ]}),
                encoding="utf-8",
            )
            return _result()

    d = Dispatcher(adapter=WritesChecks(), store=store,
                   engine=GradingEngine.default(), limits=Limits())
    report = d.run(_task(checks=()), tmp_path)
    assert report.outcome == Outcome.MERGED
    assert report.rounds == 1, "契约送达后应当一轮就过，不该再烧三轮"


def test_vacuous_worker_checks_are_rejected_not_merged(store, tmp_path):
    """worker 用 `echo ok` 糊弄契约 → 打回 vacuous-checks，不能 merge。

    这条比 no-checks-defined 更要紧：没有 check 会打回，而一条永真 check
    会**骗过**回归监工并 merge —— 那时工厂在「什么都没验」的状态下出货，
    而审计记录上写着全绿。
    """
    import json as _json

    from factory.checks_contract import CHECKS_FILENAME

    class WritesVacuous:
        name = "fake"

        def __init__(self):
            self.calls = []

        def run(self, task, workspace, limits, *, model=None):
            self.calls.append((task.prompt, model))
            (workspace / CHECKS_FILENAME).write_text(
                _json.dumps({"checks": [{"name": "ok", "command": "echo ok"}]}),
                encoding="utf-8",
            )
            return _result()

    d = Dispatcher(adapter=WritesVacuous(), store=store,
                   engine=GradingEngine.default(), limits=Limits())
    report = d.run(_task(checks=()), tmp_path)
    assert report.outcome == Outcome.ESCALATED
    claims = _verdict(store.get(report.attempt_ids[0]),
                      SupervisorRole.REGRESSION).claims
    assert claims[0]["check"] == "vacuous-checks"
