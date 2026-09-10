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


@pytest.mark.parametrize('extra,allowed', [('', True), ('\n[tool.pytest.ini_options]\naddopts="-x"\n', False)])
def test_supporting_package_resource_addition(repo, provider_module, extra, allowed):
    from factory.control.execution import _supporting_package_data
    baseline = '[tool.setuptools.package-dir]\n""="src"\n[tool.setuptools.package-data]\ngongshi=["data/*.toml"]\n'
    (repo / 'pyproject.toml').write_text(baseline)
    subprocess.run(['git', 'add', 'pyproject.toml'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'packaging'], cwd=repo, check=True)
    resource = repo / 'src/gongshi/data/prompt.md'
    resource.parent.mkdir(parents=True)
    resource.write_text('prompt')
    (repo / 'pyproject.toml').write_text(baseline.replace('"data/*.toml"', '"data/*.toml", "data/*.md"') + extra)
    assert _supporting_package_data(repo, ('src/gongshi/data/prompt.md',)) == allowed
    if allowed:
        class ResourceRunner:
            def run(self, request, emit, cancel=None):
                root = Path(request.workspace)
                resource = root / 'src/gongshi/data/prompt.md'
                resource.parent.mkdir(parents=True, exist_ok=True)
                resource.write_text('prompt')
                (root / 'pyproject.toml').write_text(baseline.replace('"data/*.toml"', '"data/*.toml", "data/*.md"'))
                return Result()
        events = []
        out = execute_plan(run_id='resource', plan={'tasks': [{'id': 'prompt', 'prompt': 'add prompt',
            'paths': ['src/gongshi/data/prompt.md'], 'checks': ['ok']}]}, project=_project(repo),
            profiles={'standard': {'provider': 'fake', 'model': 'test'}}, runner=ResourceRunner(),
            emit=lambda kind, *args: events.append(kind), cancel=threading.Event())
        assert out['tasks'][0]['status'] == 'verified'
        assert 'scope.supporting_change' in events
    resource.with_name('unrelated.md').write_text('not part of the task')
    assert not _supporting_package_data(repo, ('src/gongshi/data/prompt.md',))


def test_generated_metadata_does_not_hide_source_or_lockfile(repo):
    from factory.control.execution import _status_paths
    metadata = repo / 'src/gongshi.egg-info'
    metadata.mkdir(parents=True)
    for name in ('PKG-INFO', 'SOURCES.txt', 'entry_points.txt', 'requires.txt'):
        (metadata / name).write_text('generated')
    (repo / 'uv.lock').write_text('version = 1')
    assert _status_paths(repo, timeout_s=10) == ('uv.lock',)
    (metadata / 'injected.py').write_text('print(1)')
    assert 'src/gongshi.egg-info/injected.py' in _status_paths(repo, timeout_s=10)
    (repo / '.gitignore').write_text('*.egg-info/\n')
    assert 'src/gongshi.egg-info/injected.py' in _status_paths(repo, timeout_s=10)
    subprocess.run(['git', 'add', '-f', 'src/gongshi.egg-info/PKG-INFO'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'tracked metadata'], cwd=repo, check=True)
    (metadata / 'PKG-INFO').write_text('changed tracked metadata')
    assert 'src/gongshi.egg-info/PKG-INFO' in _status_paths(repo, timeout_s=10)


def test_autonomous_project_scope_accepts_related_source_and_lock(repo, provider_module):
    runner = FakeRunner({'work': [('base.txt', 'updated'), ('uv.lock', 'version = 1'), ('work.txt', 'done')]})
    events = []
    # FakeRunner indexes the original prompt; capture the autonomous prompt separately.
    class Runner:
        def run(self, request, emit, cancel=None):
            assert 'AUTONOMOUS PROJECT EXECUTION' in request.prompt
            for name, body in runner.writes['work']:
                Path(request.workspace, name).write_text(body)
            return Result()
    result = execute_plan(run_id='autonomous', plan={'tasks': [{'id': 'work', 'prompt': 'work', 'paths': ['work.txt'], 'checks': ['ok']}]},
        project={**_project(repo), 'autonomous_execution': True}, profiles={'standard': {'provider': 'fake', 'model': 'test'}},
        runner=Runner(), emit=lambda kind, *args: events.append(kind), cancel=threading.Event())
    assert result['tasks'][0]['status'] == 'verified'
    assert 'scope.project_changes' in events
    assert (Path(result['worktree']) / 'uv.lock').exists()


def test_autonomous_mode_does_not_allow_forbidden_files(repo, provider_module):
    with pytest.raises(ExecutionError) as error:
        execute_plan(run_id='autonomous-protected', plan={'tasks': [{'id': 'work', 'prompt': 'work', 'paths': ['work.txt'], 'checks': ['ok']}]},
            project={**_project(repo), 'autonomous_execution': True}, profiles={'standard': {'provider': 'fake', 'model': 'test'}},
            runner=type('Runner', (), {'run': lambda self, request, emit, cancel=None: (Path(request.workspace, '.env').write_text('test-only'), Result())[1]})(),
            emit=lambda *a: None, cancel=threading.Event())
    assert 'forbidden' in error.value.artifacts['tasks'][0]['error']


def test_virtualenv_interpreter_links_are_disposable_untracked_output(repo):
    from factory.control.execution import _status_paths
    bindir = repo / '.venv/bin'
    bindir.mkdir(parents=True)
    (bindir / 'python').symlink_to(sys.executable)
    assert not _status_paths(repo, timeout_s=10)
    # A tracked interpreter link must still be inspected if altered.
    subprocess.run(['git', 'add', '-f', '.venv/bin/python'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'tracked link'], cwd=repo, check=True)
    (bindir / 'python').unlink()
    (bindir / 'python').symlink_to('/different/python')
    assert '.venv/bin/python' in _status_paths(repo, timeout_s=10)


def test_ignored_delivery_outputs_do_not_break_source_commit(repo):
    from factory.control.execution import _status_paths, _commit_tree
    (repo / '.gitignore').write_text('dist/\nrelease/\nout/\nhidden.py\n')
    (repo / 'dist').mkdir()
    (repo / 'dist/index.html').write_text('<h1>built</h1>')
    (repo / 'main.py').write_text('print("source")')
    changed = _status_paths(repo, timeout_s=10)
    assert changed == ('.gitignore', 'main.py')
    _commit_tree(repo, changed, 'verified source', 10)
    assert not _status_paths(repo, timeout_s=10)
    assert (repo / 'dist/index.html').exists()
    (repo / 'hidden.py').write_text('print("ignored source")')
    assert 'hidden.py' in _status_paths(repo, timeout_s=10)
    subprocess.run(['git', 'add', '-f', 'dist/index.html'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'tracked output'], cwd=repo, check=True)
    (repo / 'dist/index.html').write_text('changed')
    assert 'dist/index.html' in _status_paths(repo, timeout_s=10)
