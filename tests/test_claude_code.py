import json
import stat
import subprocess

import pytest

from factory.harness.base import ExitStatus, Limits
from factory.harness.claude_code import ClaudeCodeAdapter
from factory.task import Task


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
    (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    return ws


def _fake_claude(tmp_path, payload: dict, *, body: str = "") -> str:
    """造一个假 claude：把 argv 落盘、可选改 workspace、打印一段 JSON。"""
    script = tmp_path / "fake_claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, pathlib\n"
        f"argv_log = pathlib.Path({str(tmp_path / 'argv.json')!r})\n"
        "argv_log.write_text(json.dumps(sys.argv[1:]))\n"
        f"{body}\n"
        f"print(json.dumps({payload!r}))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


OK_PAYLOAD = {
    "is_error": False,
    "subtype": "success",
    "session_id": "sess-abc",
    "num_turns": 3,
    "total_cost_usd": 0.0042,
    "duration_ms": 5100,
    "usage": {"input_tokens": 120, "output_tokens": 340,
              "cache_creation_input_tokens": 10,
              "cache_read_input_tokens": 20},
    "result": "done",
}


def test_run_success_captures_everything(tmp_path, repo):
    body = (
        f"pathlib.Path({str(repo / 'greet.py')!r})"
        ".write_text('def greet(n):\\n    return n\\n')\n"
    )
    binary = _fake_claude(tmp_path, OK_PAYLOAD, body=body)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")

    res = adapter.run(
        Task(task_id="T-1", prompt="add greet"),
        repo,
        Limits(max_turns=5, timeout_s=30),
        model="haiku",
    )

    assert res.exit_status == ExitStatus.OK
    assert res.ok is True
    assert res.changed_paths == ("greet.py",)
    assert "def greet" in res.diff
    assert res.diff_hash and len(res.diff_hash) == 64
    assert res.session_id == "sess-abc"
    assert (res.tokens_in, res.tokens_out) == (120, 340)
    assert res.cost_usd == pytest.approx(0.0042)
    assert res.wall_clock_ms == 5100


def test_run_passes_model_and_max_turns(tmp_path, repo):
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    adapter.run(Task(task_id="T-1", prompt="hi"), repo,
                Limits(max_turns=7), model="sonnet")

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_run_omits_optional_flags_when_unset(tmp_path, repo):
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert "--max-turns" not in argv
    assert "--model" not in argv


def test_is_error_true_maps_to_error_even_though_exit_code_is_zero(
    tmp_path, repo
):
    """claude -p 的退出码永远是 0，成功与否只能读 is_error。"""
    payload = {**OK_PAYLOAD, "is_error": True,
               "subtype": "error_max_turns", "result": "turn limit"}
    binary = _fake_claude(tmp_path, payload)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    assert res.exit_status == ExitStatus.ERROR
    assert res.ok is False
    assert "error_max_turns" in res.error_text


def test_unparseable_stdout_is_error(tmp_path, repo):
    script = tmp_path / "noisy_claude"
    script.write_text(
        "#!/usr/bin/env python3\nprint('not json at all')\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    adapter = ClaudeCodeAdapter(binary=str(script),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())
    assert res.exit_status == ExitStatus.ERROR
    assert "not json" in res.error_text


def test_timeout_maps_to_timeout_status(tmp_path, repo):
    script = tmp_path / "slow_claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    adapter = ClaudeCodeAdapter(binary=str(script),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo,
                      Limits(timeout_s=1))
    assert res.exit_status == ExitStatus.TIMEOUT


def test_missing_binary_is_error_not_crash(tmp_path, repo):
    adapter = ClaudeCodeAdapter(binary=str(tmp_path / "nonexistent-binary"),
                                projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())
    assert res.exit_status == ExitStatus.ERROR
    assert "cannot launch" in res.error_text


def test_finds_transcript_and_tool_calls(tmp_path, repo):
    proj = tmp_path / "proj" / "-some-lossy-slug"
    proj.mkdir(parents=True)
    (proj / "sess-abc.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit"}]}}) + "\n",
        encoding="utf-8",
    )
    binary = _fake_claude(tmp_path, OK_PAYLOAD)
    adapter = ClaudeCodeAdapter(binary=binary, projects_root=tmp_path / "proj")
    res = adapter.run(Task(task_id="T-1", prompt="hi"), repo, Limits())

    assert res.transcript_path == str(proj / "sess-abc.jsonl")
    assert [c.name for c in res.tool_calls] == ["Edit"]
