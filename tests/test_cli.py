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
