from __future__ import annotations

import json
from pathlib import Path

import factory.control.runtime_cli as runtime_cli


def _report() -> dict:
    return {
        "checked_at": "2026-09-08T00:00:00+00:00",
        "configuration_revision": 3,
        "profiles": {
            "planner": {"provider": "codex", "model": "gpt-test"},
            "cheap": {"provider": "claude", "model": "haiku"},
            "standard": {"provider": "codex", "model": "standard"},
            "strong": {"provider": "dsh", "model": "deepseek"},
        },
        "limits": {"timeout_s": 600, "max_parallel": 2, "max_tasks": 20},
        "tools": [
            {
                "id": "codex",
                "installed": True,
                "version": "0.1",
                "import_status": "ok",
                "auth": "missing",
                "issues": [],
            }
        ],
        "host": {
            "python_version": "3.12",
            "git_available": True,
            "node_available": True,
            "hermes_available": False,
            "frontend_built": True,
        },
        "readiness": {"planning": False, "execution": True, "publishing": False},
        "blockers": ["codex authentication is missing"],
        "last_probes": [],
    }


def test_doctor_json_is_sanitized_and_has_no_provider_call(monkeypatch, tmp_path, capsys):
    calls = []

    def inspect(settings, workspace, static):
        calls.append((settings, workspace, static))
        return {**_report(), "api_key": "do-not-print", "nested": {"token": "secret"}}

    monkeypatch.setattr(runtime_cli, "inspect_runtime", inspect)
    settings = object()
    monkeypatch.setattr(runtime_cli, "_make_settings", lambda db: settings)
    assert runtime_cli.main(
        [
            "doctor",
            "--json",
            "--workspace",
            str(tmp_path),
            "--static-dir",
            str(tmp_path / "dist"),
            "--db",
            str(tmp_path / "control.db"),
        ]
    ) == 0
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["api_key"] == "[redacted]"
    assert parsed["nested"]["token"] == "[redacted]"
    assert parsed["tools"][0]["installed"] is True
    assert calls and calls[0][1] == tmp_path.resolve()


def test_doctor_human_output_separates_package_and_auth(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runtime_cli, "inspect_runtime", lambda *args: _report())
    monkeypatch.setattr(runtime_cli, "_make_settings", lambda db: object())
    assert runtime_cli.main(["doctor", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "package=installed" in output
    assert "auth_hint=missing" in output
    assert "no paid calls" in output.lower()


def test_missing_runtime_is_a_clear_json_error(monkeypatch, capsys):
    monkeypatch.setattr(runtime_cli, "inspect_runtime", None)
    assert runtime_cli.main(["doctor", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error"].startswith("doctor failed:")


def test_install_script_is_dry_run_by_default():
    script = Path(__file__).parents[1] / "deploy" / "install-control.sh"
    text = script.read_text()
    assert "uv sync --frozen --all-extras --no-dev" in text
    assert "node ./node_modules/vite/bin/vite.js build" in text
    assert "--apply" in text
    assert "enable --now" in text  # printed next step, never run implicitly
    assert "FACTORY_PUBLIC_ORIGIN=https://harness.cloudwaveai.cn" in (
        script.parent / "control.env.example"
    ).read_text()


def test_doctor_never_creates_live_database(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_cli, 'inspect_runtime', lambda *args: _report())
    database = tmp_path / 'missing' / 'control.db'
    runtime_cli.run_doctor(db=database, workspace=tmp_path)
    assert not database.exists()
    assert not database.parent.exists()


def test_doctor_does_not_seed_existing_database(tmp_path, monkeypatch):
    from factory.control.store import Store
    database = tmp_path / 'control.db'
    store = Store(database)
    monkeypatch.setattr(runtime_cli, 'inspect_runtime', lambda *args: _report())
    runtime_cli.run_doctor(db=database, workspace=tmp_path)
    with store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='runtime_settings'").fetchone() is None


def test_doctor_exception_does_not_print_credential(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError('token=do-not-expose')
    monkeypatch.setattr(runtime_cli, 'run_doctor', fail)
    assert runtime_cli.main(['doctor', '--json']) == 1
    assert 'do-not-expose' not in capsys.readouterr().out
