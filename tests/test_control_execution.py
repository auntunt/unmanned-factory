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


@pytest.mark.parametrize('draft_status', ['failed', 'cancelled'])
def test_continue_keeps_verified_work_and_failed_draft(repo, provider_module, draft_status):
    calls = []
    class Runner:
        def run(self, request, emit, cancel=None):
            calls.append(request.prompt)
            root = Path(request.workspace)
            if request.prompt == 'first':
                (root / 'a.txt').write_text('done')
            elif request.prompt == 'second':
                assert (root / 'a.txt').read_text() == 'done'
                (root / 'a.txt').write_text('done\n\n')
                (root / 'b.txt').write_text('draft')
                (root / 'outside.txt').write_text('remove me')
            else:
                assert 'Continue the existing draft' in request.prompt
                assert (root / 'a.txt').read_text() == 'done\n\n'
                (root / 'a.txt').write_text('done')
                assert (root / 'b.txt').read_text() == 'draft'
                (root / 'outside.txt').unlink()
                (root / 'b.txt').write_text('finished')
            return Result(cost_usd=.1)
    plan = {'tasks': [
        {'id': 'a', 'prompt': 'first', 'paths': ['a.txt'], 'checks': ['ok']},
        {'id': 'b', 'prompt': 'second', 'paths': ['b.txt'], 'checks': ['ok'], 'depends_on': ['a']},
    ]}
    runner = Runner()
    with pytest.raises(ExecutionError) as failure:
        execute_plan(run_id='paused', plan=plan, project=_project(repo), profiles={'standard': {'provider': 'test', 'model': 'test'}}, runner=runner, emit=lambda *args: None, cancel=threading.Event())
    prior = failure.value.artifacts
    assert prior['tasks'][0]['status'] == 'verified'
    prior['tasks'][1]['status'] = draft_status
    continued = execute_plan(run_id='paused-c1', plan=plan, project=_project(repo), profiles={'standard': {'provider': 'test', 'model': 'test'}}, runner=runner, emit=lambda *args: None, cancel=threading.Event(), resume_artifacts=prior)
    assert len(calls) == 3 and calls.count('first') == 1
    assert (Path(continued['worktree']) / 'b.txt').read_text() == 'finished'
    assert (Path(prior['tasks'][1]['worktree']) / 'b.txt').read_text() == 'draft'
    assert len(continued['tasks'][1]['attempts']) == 2
    assert continued['known_cost_usd'] == .1


def test_python_project_metadata_allowed_but_test_configuration_protected(repo, provider_module):
    from factory.control.execution import _protected_changes
    (repo / 'pyproject.toml').write_text('[project]\nname="sample"\nversion="0.1.0"\n[build-system]\nrequires=["setuptools>=68", "wheel"]\nbuild-backend="setuptools.build_meta"\n[tool.setuptools.packages.find]\nwhere=["src"]\n')
    assert not _protected_changes(repo, ('pyproject.toml',))
    with (repo / 'pyproject.toml').open('a') as file:
        file.write('[tool.pytest.ini_options]\naddopts="--ignore=tests"\n')
    assert _protected_changes(repo, ('pyproject.toml',)) == ('pyproject.toml',)


def test_scheduler_releases_dependent_before_other_worker_finishes(repo, provider_module):
    released = threading.Event()
    class Runner:
        def run(self, request, emit, cancel=None):
            name = request.prompt
            if name == 'slow':
                assert released.wait(5), 'ready child was held behind unrelated slow task'
            if name == 'child':
                assert Path(request.workspace, 'fast.txt').exists()
                released.set()
            Path(request.workspace, name + '.txt').write_text(name)
            return Result()
    tasks = [{'id': n, 'prompt': n, 'paths': [n + '.txt'], 'checks': ['ok'],
              'depends_on': ['fast'] if n == 'child' else []} for n in ['fast', 'slow', 'child']]
    out = execute_plan(run_id='stream', plan={'tasks': tasks}, project=_project(repo),
                       profiles={'standard': {'provider': 'fake', 'model': 'test'}},
                       runner=Runner(), emit=lambda *a: None, cancel=threading.Event())
    assert all(t['status'] == 'verified' for t in out['tasks'])


def test_failed_branch_does_not_stop_independent_descendants(repo, provider_module):
    runner = FakeRunner({'bad': [('outside.txt', 'bad')], 'good': [('good.txt', 'good')],
                         'child': [('child.txt', 'child')]})
    tasks = [{'id': n, 'prompt': n, 'paths': [n + '.txt'], 'checks': ['ok'],
              'depends_on': ['good'] if n == 'child' else ['bad'] if n == 'blocked' else []}
             for n in ['bad', 'good', 'child', 'blocked']]
    with pytest.raises(ExecutionError) as error:
        execute_plan(run_id='branches', plan={'tasks': tasks}, project=_project(repo),
                     profiles={'standard': {'provider': 'fake', 'model': 'test'}}, runner=runner,
                     emit=lambda *a: None, cancel=threading.Event())
    states = {t['id']: t['status'] for t in error.value.artifacts['tasks']}
    assert states == {'bad': 'failed', 'good': 'verified', 'child': 'verified', 'blocked': 'blocked'}
    assert 'blocked' not in runner.seen
