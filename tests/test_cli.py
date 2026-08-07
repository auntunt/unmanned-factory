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
