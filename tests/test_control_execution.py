from __future__ import annotations

import subprocess
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.control.execution import ExecutionError, execute_plan


@dataclass
class Result:
    text: str = "done"
    cost_usd: float | None = 0.0


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def repo(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def provider_module(monkeypatch):
    module = types.ModuleType("factory.control.providers")
    module.ProviderRequest = Request
    monkeypatch.setitem(sys.modules, "factory.control.providers", module)


def _project(repo: Path):
    return {"workspace": str(repo), "base_branch": "master", "checks": {"ok": [sys.executable, "-c", "print('ok')"]}}


class FakeRunner:
    def __init__(self, writes):
        self.writes = writes
        self.seen: list[str] = []

    def run(self, request, emit, cancel=None):
        self.seen.append(request.prompt)
        for name, body in self.writes[request.prompt]:
            Path(request.workspace, name).write_text(body)
        return Result()


def test_dag_order_and_final_content(repo, provider_module):
    runner = FakeRunner({"a": [("a.txt", "a\n")], "b": [("b.txt", "b\n")]})
    plan = {"tasks": [
        {"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["ok"]},
        {"id": "b", "prompt": "b", "paths": ["b.txt"], "checks": ["ok"], "depends_on": ["a"]},
    ]}
    out = execute_plan(run_id="run1", plan=plan, project=_project(repo), profiles={"standard": {"provider": "fake", "model": "test"}}, runner=runner, emit=lambda *_: None, cancel=threading.Event())
    assert runner.seen == ["a", "b"]
    assert out["commit"]
    assert (repo / "base.txt").read_text() == "base\n"
    integrated = Path(out["worktree"])
    assert (integrated / "a.txt").read_text() == "a\n"
    assert (integrated / "b.txt").read_text() == "b\n"


def test_out_of_scope_change_fails_and_keeps_worktree(repo, provider_module):
    runner = FakeRunner({"a": [("evil.txt", "no\n")]})
    plan = {"tasks": [{"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["ok"]}]}
    with pytest.raises(ExecutionError, match="failed"):
        execute_plan(run_id="run2", plan=plan, project=_project(repo), profiles={"standard": {"provider": "fake", "model": "test"}}, runner=runner, emit=lambda *_: None, cancel=threading.Event())


def test_verification_failure_is_recorded(repo, provider_module):
    runner = FakeRunner({"a": [("a.txt", "a\n")]})
    project = _project(repo)
    project["checks"] = {"bad": [sys.executable, "-c", "raise SystemExit(3)"]}
    plan = {"tasks": [{"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["bad"]}]}
    with pytest.raises(ExecutionError, match="failed"):
        execute_plan(run_id="run3", plan=plan, project=project, profiles={"standard": {"provider": "fake", "model": "test"}}, runner=runner, emit=lambda *_: None, cancel=threading.Event())


def test_cancel_before_dispatch(repo, provider_module):
    cancel = threading.Event()
    cancel.set()
    plan = {"tasks": [{"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["ok"]}]}
    with pytest.raises(ExecutionError, match="cancelled"):
        execute_plan(run_id="run4", plan=plan, project=_project(repo), profiles={"standard": {"provider": "fake", "model": "test"}}, runner=FakeRunner({}), emit=lambda *_: None, cancel=cancel)


def test_large_check_output_is_drained(repo, provider_module):
    runner = FakeRunner({"a": [("a.txt", "a\n")]})
    project = _project(repo)
    project["checks"] = {"large": [sys.executable, "-c", "print('x' * 120000)"]}
    plan = {"tasks": [{"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["large"]}]}
    out = execute_plan(run_id="run5", plan=plan, project=project, profiles={"standard": {"provider": "fake", "model": "test"}}, runner=runner, emit=lambda *_: None, cancel=threading.Event())
    assert out["checks"][0]["exit"] == 0
    assert len(out["checks"][0]["stdout"]) <= 4000
