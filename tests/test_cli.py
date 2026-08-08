import argparse
import stat
import subprocess

import pytest

from factory.audit.store import AuditStore
from factory.cli import main


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    return ws


@pytest.fixture
def task_file(tmp_path):
    p = tmp_path / "task.yaml"
    p.write_text(
        "task_id: T-cli-1\n"
        "prompt: create greet.py\n"
        "spec_ref: [AC-1]\n"
        "declared_paths: ['greet.py']\n"
        "checks:\n"
        "  - name: greet-exists\n"
        "    command: test -f greet.py\n",
        encoding="utf-8",
    )
    return p


def _fake_claude(tmp_path, repo):
    script = tmp_path / "fake_claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(repo / 'greet.py')!r}).write_text("
        "'def greet(n):\\n    return f\"Hello, {n}!\"\\n')\n"
        "print(json.dumps({'is_error': False, 'session_id': 'sess-cli',\n"
        "  'total_cost_usd': 0.001, 'duration_ms': 900,\n"
        "  'usage': {'input_tokens': 5, 'output_tokens': 7}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_run_merges_and_returns_zero(tmp_path, repo, task_file, capsys):
    db = tmp_path / "audit.db"
    code = main([
        "run", str(task_file), "--workspace", str(repo),
        "--db", str(db), "--binary", _fake_claude(tmp_path, repo),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "merged" in out

    row = AuditStore(db).get(1)
    assert row.task_id == "T-cli-1"
    assert row.resolution == "merged"
    assert row.model == "haiku"
    assert row.diff_hash is not None


def test_run_hard_gate_returns_nonzero(tmp_path, repo, capsys):
    p = tmp_path / "d.yaml"
    p.write_text(
        "task_id: T-d-1\nprompt: deploy it\n"
        "declared_ops: [prod_deploy]\n"
        "checks:\n  - name: noop\n    command: 'true'\n",
        encoding="utf-8",
    )
    code = main(["run", str(p), "--workspace", str(repo),
                 "--db", str(tmp_path / "a.db")])
    assert code == 1
    out = capsys.readouterr().out
    assert "blocked_hard_gate" in out
    assert "硬闸门" in out


def test_show_prints_audit_trail(tmp_path, repo, task_file, capsys):
    db = tmp_path / "audit.db"
    main(["run", str(task_file), "--workspace", str(repo), "--db", str(db),
          "--binary", _fake_claude(tmp_path, repo)])
    capsys.readouterr()

    assert main(["show", "T-cli-1", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "T-cli-1" in out
    assert "regression" in out
    assert "merged" in out


def test_show_unknown_task_returns_nonzero(tmp_path, capsys):
    assert main(["show", "T-nope", "--db", str(tmp_path / "a.db")]) == 1
    assert "没有" in capsys.readouterr().out


# ── 沙箱三态：默认开、显式关、平台不支持 ──────────────────────────────────

def _ns(**kw):
    """造一个只带 sandbox 字段的 Namespace，直接测解析逻辑。"""
    import argparse
    return argparse.Namespace(**kw)


def test_sandbox_defaults_to_on_when_available(monkeypatch, capsys):
    """默认开。无人工厂里"靠人记得加 flag"的防护等于没有防护。"""
    from factory import cli
    from factory.harness import sandbox as sb

    monkeypatch.setattr(sb, "available", lambda: True)
    ns = _ns(sandbox=None)
    assert cli._resolve_sandbox(ns) == 0
    assert ns.sandbox is True


def test_explicit_sandbox_flag_errors_on_unsupported_platform(monkeypatch, capsys):
    """显式 --sandbox 而平台不支持 → 退出码 2，不静默降级。

    静默降级是这里最坏的结果：人以为隔离生效，实际上 worker 在裸奔。
    比"跑不起来"危险，因为跑不起来会被立刻发现。
    """
    from factory import cli
    from factory.harness import sandbox as sb

    monkeypatch.setattr(sb, "available", lambda: False)
    ns = _ns(sandbox=True)
    assert cli._resolve_sandbox(ns) == 2
    assert "不静默降级" in capsys.readouterr().err


def test_sandbox_silently_off_on_unsupported_platform(monkeypatch, capsys):
    """没显式要求时，平台不支持就关掉并提示 —— 好让流水线在 Linux 上仍能跑。"""
    from factory import cli
    from factory.harness import sandbox as sb

    monkeypatch.setattr(sb, "available", lambda: False)
    ns = _ns(sandbox=None)
    assert cli._resolve_sandbox(ns) == 0
    assert ns.sandbox is False
    assert "未隔离" in capsys.readouterr().err


def test_no_sandbox_warns_about_what_it_gives_up(monkeypatch, capsys):
    """关沙箱要把代价打出来，不能静悄悄地关。"""
    from factory import cli

    ns = _ns(sandbox=False)
    assert cli._resolve_sandbox(ns) == 0
    assert ns.sandbox is False
    err = capsys.readouterr().err
    assert "分级规则" in err, "提示里要说清 worker 能改分级规则"


def test_run_records_sandbox_in_audit(tmp_path, repo, task_file):
    """端到端：默认（不给 flag）跑一次，审计里应留下 +sandbox 痕迹。

    这条把"默认开"钉在真派发路径上，而不只是解析函数上 —— 解析对了但没有
    传下去的话，前面那些单测照样全绿。
    """
    import sys as _sys

    db = tmp_path / "audit.db"
    assert main([
        "run", str(task_file), "--workspace", str(repo),
        "--db", str(db), "--binary", _fake_claude(tmp_path, repo),
    ]) == 0
    version = AuditStore(db).get(1).harness_version
    if _sys.platform == "darwin":
        assert version.endswith("+sandbox"), f"默认没开沙箱：{version}"
    else:
        assert not version.endswith("+sandbox")


def test_no_sandbox_flag_is_recorded_too(tmp_path, repo, task_file):
    """反向：显式 --no-sandbox 时审计里不该有 +sandbox。

    没有这条，上一条无法区分"默认开生效了"和"后缀永远都在"。
    """
    db = tmp_path / "audit.db"
    assert main([
        "run", str(task_file), "--workspace", str(repo), "--no-sandbox",
        "--db", str(db), "--binary", _fake_claude(tmp_path, repo),
    ]) == 0
    assert not AuditStore(db).get(1).harness_version.endswith("+sandbox")


def test_loop_refuses_to_start_when_the_worker_binary_is_missing(
        tmp_path, repo, monkeypatch, capsys):
    """launchd 场景：PATH 里没有 claude。必须在进入循环**之前**退出。

    不拦的话，队列里每个任务都会被派发、失败、刷成 error —— 而 version()
    吞掉 OSError 只回 "unknown"，日志里没有任何指向「binary 找不到」的信号。
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    q = tmp_path / "q"
    code = main(["loop", "--queue", str(q), "--workspace", str(repo),
                 "--db", str(tmp_path / "audit.db"), "--idle", "drain",
                 "--no-sandbox"])
    assert code == 2
    err = capsys.readouterr().err
    assert "claude" in err and "PATH" in err


def test_loop_preflight_runs_before_the_queue_is_even_created(
        tmp_path, repo, monkeypatch):
    """预检要在 Backlog.ensure() 之前 —— 否则一次配置错误会留下一地空目录。

    这条不只是洁癖：`queue` 子命令看到目录存在就认为队列已初始化，
    人会以为「队列是好的，任务没进来」，而真相是 loop 一次都没跑起来。
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    q = tmp_path / "q-never"
    assert main(["loop", "--queue", str(q), "--workspace", str(repo),
                 "--db", str(tmp_path / "audit.db"), "--idle", "drain",
                 "--no-sandbox"]) == 2
    assert not q.exists(), "配置错误不该留下半初始化的队列目录"


def test_loop_preflight_checks_shell_argv_not_the_claude_binary(
        tmp_path, repo, monkeypatch, capsys):
    """--harness shell 时该查的是 shell_argv[0]。

    查 ns.binary 会让 shell harness 在**没装 claude 的机器上**过不了预检，
    而它根本不需要 claude；反过来，脚本不存在却因为 claude 在就放行，
    等于预检对这条路径完全失效。
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    code = main(["loop", "--queue", str(tmp_path / "q"),
                 "--workspace", str(repo), "--db", str(tmp_path / "a.db"),
                 "--idle", "drain", "--no-sandbox",
                 "--harness", "shell",
                 "--shell-argv", str(tmp_path / "nope.sh"), "{prompt}"])
    assert code == 2
    assert "nope.sh" in capsys.readouterr().err


def test_the_factory_console_script_is_declared(tmp_path):
    """文档里从第一天就写着 `factory run ...`，但入口点一直不存在。

    `uv run factory --help` 报的是 "Failed to spawn: factory" —— 也就是说
    README、设计文档、plist 里每一条示例命令都跑不起来，而没有任何测试会发现，
    因为测试全部直接 import `main`。这条钉住声明本身。
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["project"]["scripts"]["factory"] == "factory.cli:main"


def test_main_returns_an_int_so_the_console_script_can_exit_with_it(
        tmp_path, capsys):
    # console script 的返回值直接进 sys.exit。返回 None 会变成退出码 0,
    # 于是 cron 认为「失败的那次也成功了」。
    rc = main(["show", "T-does-not-exist", "--db", str(tmp_path / "a.db")])
    assert isinstance(rc, int) and rc != 0


def _row(cost, claims_got=None, attempt_no=1, transcript=None):
    class V:
        claims = [{"got": g} for g in (claims_got or [])]
    class R:
        cost_usd = cost
        supervisors = [V()]
        transcript_path = transcript
    R.attempt_no = attempt_no
    return R()


def test_a_timed_out_attempt_recording_zero_is_warned_about(capsys):
    """真跑实测的漏账：900s 超时的 attempt 记 $0，transcript 里有 ~232k tokens。

    花费只来自 claude CLI 的 JSON payload，而被 kill 的进程永远不打那个
    payload。预算闸门累加的就是这些数，所以一个反复超时的任务在预算眼里是
    免费的 —— 「低估的预算闸门等于没有闸门」在这条路径上再次成立。
    """
    from factory.cli import _warn_if_untracked_spend

    _warn_if_untracked_spend(
        _row(0.0, ["timeout: timeout after 900s"], transcript="/tmp/t.jsonl"))
    err = capsys.readouterr().err
    assert "超时" in err and "预算" in err
    assert "/tmp/t.jsonl" in err, "要给出 transcript 路径，否则人无法核实花了多少"


def test_an_attempt_that_actually_cost_money_is_not_warned_about(capsys):
    from factory.cli import _warn_if_untracked_spend

    _warn_if_untracked_spend(_row(2.4579, ["timeout: timeout after 900s"]))
    assert capsys.readouterr().err == ""


def test_a_zero_cost_attempt_that_did_not_time_out_is_not_warned_about(capsys):
    # 确定性监工的 attempt 本来就是 $0（regression/scope 不调模型）。
    # 对它们报警会让这行警告变成噪音，而噪音等于没有警告。
    from factory.cli import _warn_if_untracked_spend

    _warn_if_untracked_spend(_row(0.0, ["expected exit 0, got 1"]))
    assert capsys.readouterr().err == ""


def test_an_attempt_with_no_claims_at_all_does_not_crash(capsys):
    from factory.cli import _warn_if_untracked_spend

    _warn_if_untracked_spend(_row(0.0, []))
    assert capsys.readouterr().err == ""


def test_loop_warns_when_there_is_no_wall_clock_bound(tmp_path, repo, capsys):
    """预算不是充分的上限 —— 超时记 $0，反复超时的任务在预算眼里免费。

    墙钟是超时唯一躲不过的那道闸，所以没给的时候要说出来。
    """
    code = main(["loop", "--queue", str(tmp_path / "q"),
                 "--workspace", str(repo), "--db", str(tmp_path / "a.db"),
                 "--idle", "drain", "--no-sandbox", "--budget-usd", "5"])
    assert code == 0
    err = capsys.readouterr().err
    assert "--max-runtime 0" in err and "$0" in err


def test_loop_does_not_warn_when_a_wall_clock_bound_is_given(
        tmp_path, repo, capsys):
    code = main(["loop", "--queue", str(tmp_path / "q"),
                 "--workspace", str(repo), "--db", str(tmp_path / "a.db"),
                 "--idle", "drain", "--no-sandbox", "--budget-usd", "5",
                 "--max-runtime", "14400"])
    assert code == 0
    assert "--max-runtime 0" not in capsys.readouterr().err


# ── 熔断器的 CLI 接线 ────────────────────────────────────────────────────────
# 循环那一侧的闸门有自己的测试；这两条测的是「flag 真的接上了」和「漏账
# 真的传下去了」。写它们的直接原因：把这两处接线各删一次，整个测试套件
# 62 + 21 条全过 —— 一道谁都没接上的闸门在报表上和接上了长得一样。

def _captured_limits(argv, tmp_path, monkeypatch):
    """跑一次 loop，把 CLI 造出来的 LoopLimits 截下来。"""
    import factory.cli as cli
    seen = {}

    class Spy(cli.BacklogLoop):
        def __init__(self, *a, **kw):
            seen["limits"] = kw.get("limits")
            super().__init__(*a, **kw)

    monkeypatch.setattr(cli, "BacklogLoop", Spy)
    assert main(argv) == 0
    return seen["limits"]


def test_the_unpriced_streak_flag_reaches_the_loop(tmp_path, repo, monkeypatch):
    limits = _captured_limits(
        ["loop", "--queue", str(tmp_path / "q"), "--workspace", str(repo),
         "--db", str(tmp_path / "a.db"), "--idle", "drain", "--no-sandbox",
         "--max-unpriced-streak", "7"], tmp_path, monkeypatch)
    assert limits.max_unpriced_streak == 7


def test_the_breaker_is_on_by_default_from_the_command_line(
        tmp_path, repo, monkeypatch):
    """默认值写在 dataclass 上不算数 —— argparse 的 default 漏了同样是关着的。"""
    limits = _captured_limits(
        ["loop", "--queue", str(tmp_path / "q"), "--workspace", str(repo),
         "--db", str(tmp_path / "a.db"), "--idle", "drain", "--no-sandbox"],
        tmp_path, monkeypatch)
    assert limits.max_unpriced_streak >= 1


def test_loop_warns_when_the_breaker_is_switched_off(tmp_path, repo, capsys):
    code = main(["loop", "--queue", str(tmp_path / "q"),
                 "--workspace", str(repo), "--db", str(tmp_path / "a.db"),
                 "--idle", "drain", "--no-sandbox", "--max-runtime", "60",
                 "--max-unpriced-streak", "0"])
    assert code == 0
    assert "--max-unpriced-streak 0" in capsys.readouterr().err


def _dispatch_with_cost(tmp_path, repo, task_file, monkeypatch, cost_result):
    """跑一次 _dispatch_queued，把 _attempts_cost 的返回值换成给定的。"""
    import factory.cli as cli

    class FakeReport:
        outcome = "merged"
        attempt_ids = (1,)
        escalation_reason = ""
        commit = ""
        landing_note = ""

    class FakeDispatcher:
        def run(self, _task, _ws):
            return FakeReport()

    monkeypatch.setattr(cli, "_dispatcher_for", lambda _ns: FakeDispatcher())
    monkeypatch.setattr(cli, "_attempts_cost", lambda _ns, _ids: cost_result)
    # dispatcher 已被替掉，_dispatch_queued 只还需要 workspace（pool=None 时
    # _queued_workspace 直接返回它）。手搭 Namespace 比走 argparse 少一层耦合。
    ns = argparse.Namespace(workspace=str(repo), db=str(tmp_path / "a.db"))
    return cli._dispatch_queued(ns, task_file, None)


def test_an_unpriced_dispatch_is_reported_as_such_to_the_loop(
        tmp_path, repo, task_file, monkeypatch):
    """漏账在 _attempts_cost 里查出来没用 —— 熔断器在循环里，得传过去。"""
    run = _dispatch_with_cost(tmp_path, repo, task_file, monkeypatch,
                              (0.0, True))
    assert run.unpriced is True


def test_a_normally_priced_dispatch_is_not_flagged(
        tmp_path, repo, task_file, monkeypatch):
    run = _dispatch_with_cost(tmp_path, repo, task_file, monkeypatch,
                              (0.42, False))
    assert run.unpriced is False and run.cost_usd == 0.42
