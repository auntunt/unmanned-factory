from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.control.execution import ExecutionError, execute_plan, _check_argv, _normalize_check_argv
from factory.control.providers import ProviderError


@dataclass
class Result:
    text: str = "done"
    cost_usd: float | None = 0.0
    session_id: str | None = None


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def repo(tmp_path: Path):
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp_path, check=True)
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


def test_dag_passes_decreasing_remaining_budget_to_dependent_calls(repo, provider_module):
    calls = []
    class BudgetRunner:
        def run(self, request, emit, cancel=None):
            calls.append(request)
            Path(request.workspace, request.prompt + '.txt').write_text(request.prompt)
            return Result(cost_usd=.2)
    plan = {'tasks': [
        {'id': 'a', 'prompt': 'a', 'paths': ['a.txt'], 'checks': ['ok']},
        {'id': 'b', 'prompt': 'b', 'paths': ['b.txt'], 'checks': ['ok'],
         'depends_on': ['a']},
    ]}
    execute_plan(run_id='budgeted-dag', plan=plan,
        project={**_project(repo), 'budget_usd': 1.0},
        profiles={'standard': {'provider': 'claude', 'model': 'test'}},
        runner=BudgetRunner(), emit=lambda *_: None, cancel=threading.Event())
    assert [request.max_budget_usd for request in calls] == pytest.approx([1.0, .8])


def test_budgeted_independent_dag_calls_are_serial_and_share_remaining_budget(repo, provider_module):
    calls = []
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    class BudgetRunner:
        def run(self, request, emit, cancel=None):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                calls.append(request)
            time.sleep(.05)
            Path(request.workspace, request.prompt + '.txt').write_text(request.prompt)
            with state_lock:
                active -= 1
            return Result(cost_usd=.2)

    plan = {'tasks': [
        {'id': 'a', 'prompt': 'a', 'paths': ['a.txt'], 'checks': ['ok']},
        {'id': 'b', 'prompt': 'b', 'paths': ['b.txt'], 'checks': ['ok']},
    ]}
    execute_plan(run_id='parallel-budgeted-dag', plan=plan,
        project={**_project(repo), 'budget_usd': 1.0},
        profiles={'standard': {'provider': 'claude', 'model': 'test'}},
        runner=BudgetRunner(), emit=lambda *_: None, cancel=threading.Event(),
        max_parallel=2)
    assert max_active == 1
    assert [request.max_budget_usd for request in calls] == pytest.approx([1.0, .8])


def test_unbudgeted_independent_dag_calls_remain_parallel(repo, provider_module):
    rendezvous = threading.Barrier(2)

    class ParallelRunner:
        def run(self, request, emit, cancel=None):
            rendezvous.wait(timeout=2)
            Path(request.workspace, request.prompt + '.txt').write_text(request.prompt)
            return Result(cost_usd=.2)

    plan = {'tasks': [
        {'id': 'a', 'prompt': 'a', 'paths': ['a.txt'], 'checks': ['ok']},
        {'id': 'b', 'prompt': 'b', 'paths': ['b.txt'], 'checks': ['ok']},
    ]}
    project = {**_project(repo), 'budget_usd': None}
    execute_plan(run_id='parallel-unbudgeted-dag', plan=plan, project=project,
        profiles={'standard': {'provider': 'claude', 'model': 'test'}},
        runner=ParallelRunner(), emit=lambda *_: None, cancel=threading.Event(),
        max_parallel=2)


@pytest.mark.parametrize(('callback_session', 'error_session', 'expected'), [
    ('callback-session', None, 'callback-session'),
    (None, 'error-session', 'error-session'),
])
def test_transient_provider_error_resumes_retained_session(
        repo, provider_module, callback_session, error_session, expected):
    calls = []

    class RetryRunner:
        def run(self, request, emit, cancel=None):
            calls.append(request)
            if len(calls) == 1:
                if callback_session:
                    emit('provider.session', {'session_id': callback_session})
                emit('provider.usage', {'cost_usd': .1})
                raise ProviderError('temporary outage', session_id=error_session, transient=True)
            assert request.session_id == expected
            Path(request.workspace, 'a.txt').write_text('done')
            return Result(cost_usd=.1, session_id=expected)

    plan = {'tasks': [
        {'id': 'a', 'prompt': 'a', 'paths': ['a.txt'], 'checks': ['ok']},
    ]}
    result = execute_plan(run_id=f'resume-{expected}', plan=plan,
        project={**_project(repo), 'routing_policy': {'max_attempts': 2, 'auto_escalate': False}},
        profiles={'standard': {'provider': 'claude', 'model': 'test'}},
        runner=RetryRunner(), emit=lambda *_: None, cancel=threading.Event())
    task = result['tasks'][0]
    assert [request.session_id for request in calls] == [None, expected]
    assert task['attempts'][0]['session_id'] == expected
    assert task['session_id'] == expected


def test_provider_budget_stop_preserves_exception_session(repo, provider_module):
    class BudgetRunner:
        def run(self, request, emit, cancel=None):
            emit('provider.usage', {'cost_usd': request.max_budget_usd})
            raise ProviderError('call budget reached', session_id='budget-session',
                                transient=False, error_kind='budget_exhausted')

    plan = {'tasks': [
        {'id': 'a', 'prompt': 'a', 'paths': ['a.txt'], 'checks': ['ok']},
    ]}
    with pytest.raises(ExecutionError, match='task a failed') as caught:
        execute_plan(run_id='budget-session', plan=plan,
            project={**_project(repo), 'budget_usd': .5},
            profiles={'standard': {'provider': 'claude', 'model': 'test'}},
            runner=BudgetRunner(), emit=lambda *_: None, cancel=threading.Event())
    task = caught.value.artifacts['tasks'][0]
    assert task['session_id'] == 'budget-session'
    assert task['attempts'][0]['session_id'] == 'budget-session'


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


# ---------------------------------------------------------------------------
# T07: _check_argv normalization and validation
# ---------------------------------------------------------------------------


class TestCheckArgvNormalization:
    """Unit tests for _check_argv and _normalize_check_argv."""

    def test_proper_argv_passes_through(self):
        """Multi-element argv list is passed through unchanged."""
        project = {"checks": {"test": [sys.executable, "-c", "print('ok')"]}}
        result = _check_argv(project, ["test"])
        assert result == [("test", [sys.executable, "-c", "print('ok')"])]

    def test_single_string_with_spaces_is_split(self):
        """Single-element list with spaces is normalized via shlex.split."""
        project = {"checks": {"test": ["python3 -m pytest test_counter.py -v"]}}
        result = _check_argv(project, ["test"])
        name, argv = result[0]
        assert name == "test"
        assert argv == ["python3", "-m", "pytest", "test_counter.py", "-v"]

    def test_single_string_without_spaces_passes_through(self):
        """Single-element list without spaces is not altered."""
        project = {"checks": {"lint": ["ruff"]}}
        result = _check_argv(project, ["lint"])
        assert result == [("lint", ["ruff"])]

    def test_single_string_with_quotes_is_split(self):
        """Shell quoting in a single-element string is handled by shlex."""
        project = {"checks": {"test": ["python3 -c 'print(\"hello world\")'"]}}
        result = _check_argv(project, ["test"])
        _, argv = result[0]
        assert argv == ["python3", "-c", 'print("hello world")']

    def test_missing_check_name_raises(self):
        project = {"checks": {"test": [sys.executable, "-c", "pass"]}}
        with pytest.raises(ExecutionError, match="not a trusted argv"):
            _check_argv(project, ["missing"])

    def test_empty_list_raises(self):
        project = {"checks": {"bad": []}}
        with pytest.raises(ExecutionError, match="not a trusted argv"):
            _check_argv(project, ["bad"])

    def test_non_string_element_raises_structured_error(self):
        project = {"checks": {"bad": ["python3", 42]}}
        with pytest.raises(ExecutionError, match="invalid elements"):
            _check_argv(project, ["bad"])

    def test_empty_string_element_raises_structured_error(self):
        project = {"checks": {"bad": ["python3", ""]}}
        with pytest.raises(ExecutionError, match="invalid elements"):
            _check_argv(project, ["bad"])

    def test_nul_in_argv_raises(self):
        project = {"checks": {"bad": ["python3", "-c\x00evil"]}}
        with pytest.raises(ExecutionError, match="NUL"):
            _check_argv(project, ["bad"])

    def test_checks_not_mapping_raises(self):
        project = {"checks": "not a dict"}
        with pytest.raises(ExecutionError, match="must be a mapping"):
            _check_argv(project, ["x"])

    def test_unparseable_shell_string_raises(self):
        """Unbalanced quotes in a single-element string produce a structured error."""
        project = {"checks": {"bad": ["python3 -c 'unterminated"]}}
        with pytest.raises(ExecutionError, match="cannot parse shell string"):
            _check_argv(project, ["bad"])


class TestCheckArgvSpacedPaths:
    """T11: single-element argv that is a real executable with spaces must not be split."""

    def test_absolute_path_with_space_preserved(self, tmp_path):
        """An absolute path containing spaces pointing to an existing executable is kept intact."""
        spaced = tmp_path / "My Tools"
        spaced.mkdir()
        checker = spaced / "checker"
        checker.write_text("#!/bin/sh\nexit 0\n")
        checker.chmod(0o755)
        argv_str = str(checker)
        project = {"checks": {"test": [argv_str]}}
        result = _check_argv(project, ["test"], root=tmp_path)
        name, argv = result[0]
        assert name == "test"
        assert argv == [argv_str], f"Expected single-element argv preserved, got {argv}"

    def test_absolute_nonexistent_path_with_space_is_split(self, tmp_path):
        """A single string with spaces that does NOT point to an existing file is still shlex.split'd."""
        project = {"checks": {"test": ["python3 -m pytest test_counter.py"]}}
        result = _check_argv(project, ["test"], root=tmp_path)
        _, argv = result[0]
        assert argv == ["python3", "-m", "pytest", "test_counter.py"]

    def test_relative_path_with_space_resolved_against_root(self, tmp_path):
        """A relative path with spaces resolved against root is kept intact when it exists."""
        spaced = tmp_path / "my scripts"
        spaced.mkdir()
        checker = spaced / "run"
        checker.write_text("#!/bin/sh\nexit 0\n")
        checker.chmod(0o755)
        project = {"checks": {"test": ["my scripts/run"]}}
        result = _check_argv(project, ["test"], root=tmp_path)
        _, argv = result[0]
        assert argv == ["my scripts/run"], f"Expected relative spaced path preserved, got {argv}"

    def test_real_subprocess_with_spaced_path(self, repo, provider_module):
        """Full integration: a check whose argv[0] has spaces runs exit 0."""
        spaced = repo / "My Tools"
        spaced.mkdir()
        checker = spaced / "checker"
        checker.write_text("#!/bin/sh\nexit 0\n")
        checker.chmod(0o755)
        runner = FakeRunner({"a": [("a.txt", "a\n")]})
        project = _project(repo)
        project["checks"] = {"ok": [str(checker)]}
        plan = {"tasks": [
            {"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["ok"]},
        ]}
        out = execute_plan(
            run_id="spaced-path-check", plan=plan, project=project,
            profiles={"standard": {"provider": "fake", "model": "test"}},
            runner=runner, emit=lambda *_: None, cancel=threading.Event(),
        )
        task_checks = out["tasks"][0]["checks"]
        assert any(c["exit"] == 0 for c in task_checks), f"Expected exit 0 from spaced path, got {task_checks}"


class TestCheckArgvRealSubprocess:
    """Integration: normalized argv actually runs via real subprocess."""

    def test_shell_string_check_runs_successfully(self, repo, provider_module):
        """A check written as a single shell string runs after normalization."""
        runner = FakeRunner({"a": [("a.txt", "a\n")]})
        project = _project(repo)
        # Override with a single-string check that would have caused Errno 2 before.
        # Use double-quoted inner string so shlex does not strip quotes needed by Python.
        project["checks"] = {"ok": [f'{sys.executable} -c "print(42)"']}
        plan = {"tasks": [
            {"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["ok"]},
        ]}
        out = execute_plan(
            run_id="shell-string-check", plan=plan, project=project,
            profiles={"standard": {"provider": "fake", "model": "test"}},
            runner=runner, emit=lambda *_: None, cancel=threading.Event(),
        )
        # Check executed successfully (exit 0)
        task_checks = out["tasks"][0]["checks"]
        assert any(c["exit"] == 0 for c in task_checks), f"Expected exit 0, got {task_checks}"

    def test_proper_argv_check_still_works(self, repo, provider_module):
        """Normal multi-element argv still works as before."""
        runner = FakeRunner({"a": [("a.txt", "a\n")]})
        project = _project(repo)
        plan = {"tasks": [
            {"id": "a", "prompt": "a", "paths": ["a.txt"], "checks": ["ok"]},
        ]}
        out = execute_plan(
            run_id="proper-argv-check", plan=plan, project=project,
            profiles={"standard": {"provider": "fake", "model": "test"}},
            runner=runner, emit=lambda *_: None, cancel=threading.Event(),
        )
        task_checks = out["tasks"][0]["checks"]
        assert any(c["exit"] == 0 for c in task_checks)


def test_dockerfile_check_builds_and_runs_in_container(tmp_path, monkeypatch):
    from factory.control.execution import _run_check_unlimited
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'Dockerfile').write_text('FROM python:3.12\n')
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    calls = tmp_path / 'docker-calls.jsonl'
    docker = fake_bin / 'docker'
    docker.write_text(
        f'#!{sys.executable}\n'
        'import json, pathlib, sys\n'
        f'with pathlib.Path({str(calls)!r}).open("a") as log:\n'
        '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
    )
    docker.chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake_bin}:/usr/bin:/bin')
    record = _run_check_unlimited(root, 'pytest', ['@dockerfile', 'python3', '-m', 'pytest', '-q'],
                                  10, lambda *_: None, None)
    assert record['exit'] == 0 and record['backend'] == 'dockerfile'
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [command[0] for command in commands] == ['build', 'run', 'image']
    assert '--network=none' in commands[0]
    run = commands[1]
    assert '--network=none' in run and '--read-only' in run
    assert '--user' in run and '65534:65534' in run
    assert run[run.index('--entrypoint') + 1] == '/bin/sh'
    assert '--mount' in run and ',readonly' in run[run.index('--mount') + 1]
    assert any('cp -R /workspace/. /tmp/webuddy-workspace/' in part and 'exec "$@"' in part
               for part in run)
    assert run[-4:] == ['python3', '-m', 'pytest', '-q']


def test_dockerfile_check_never_falls_back_to_host(tmp_path, monkeypatch):
    from factory.control.execution import _check_launch_error, _run_check_unlimited
    (tmp_path / 'Dockerfile').write_text('FROM python:3.12\n')
    empty = tmp_path / 'empty-bin'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    assert 'Docker CLI 不可用' in _check_launch_error(tmp_path,
                                                     ['@dockerfile', 'python3', '-m', 'pytest', '-q'])
    record = _run_check_unlimited(tmp_path, 'pytest', ['@dockerfile', 'python3', '-m', 'pytest', '-q'],
                                  10, lambda *_: None, None)
    assert record['exit'] == 127 and '不会退回宿主机' in record['stderr']


def test_dockerfile_check_stops_when_image_build_fails(tmp_path, monkeypatch):
    from factory.control.execution import _run_check_unlimited
    (tmp_path / 'Dockerfile').write_text('FROM python:3.12\n')
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    docker = fake_bin / 'docker'
    docker.write_text('#!/bin/sh\n[ "$1" = build ] && exit 42\nexit 0\n')
    docker.chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake_bin}:/usr/bin:/bin')
    record = _run_check_unlimited(tmp_path, 'pytest', ['@dockerfile', 'python3', '-m', 'pytest', '-q'],
                                  10, lambda *_: None, None)
    assert record['exit'] == 42 and '容器镜像构建失败' in record['stderr']
