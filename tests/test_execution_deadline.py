"""Shared execution deadline and malformed provider billing regression cases."""
import json
import math
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import factory.control.execution as execution
from tests.test_runtime_execution import ExecutionRunner, _git_repo, _profiles, _project, _task


def test_commit_git_subcalls_share_one_deadline(monkeypatch, tmp_path):
    clock = [0.0]
    observed = []
    monkeypatch.setattr(execution, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def run(argv, *, timeout_s, **kwargs):
        observed.append(timeout_s)
        clock[0] += 2
        if '--name-only' in argv:
            output = 'first.txt\0'
        elif 'write-tree' in argv or 'HEAD^{tree}' in argv:
            output = 'same-tree\n'
        else:
            output = 'commit\n'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(execution, 'run_bounded', run)
    @execution._with_execution_budget
    def commit(*, cancel, timeout_s):
        return execution._commit_tree(tmp_path, ('first.txt',), 'test', 100)
    assert commit(cancel=threading.Event(), timeout_s=15) == 'commit'
    assert observed == [15, 13, 11, 9, 7, 5]
    # The request-local budget cannot leak into the next use of this thread.
    execution._git(tmp_path, 'status', timeout_s=100)
    assert observed[-1] == 100


def test_git_deadline_stops_before_commit_can_continue(monkeypatch, tmp_path):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(execution, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def run(argv, **kwargs):
        calls.append(argv)
        clock[0] += 2
        return SimpleNamespace(returncode=0, stdout='first.txt\0', stderr='')
    monkeypatch.setattr(execution, 'run_bounded', run)
    cancel = threading.Event()
    @execution._with_execution_budget
    def commit(*, cancel, timeout_s):
        execution._commit_tree(tmp_path, ('first.txt',), 'test', 100)
    with pytest.raises(execution.ExecutionError, match='execution timeout'):
        commit(cancel=cancel, timeout_s=3)
    assert cancel.is_set()
    assert len(calls) == 2
    assert not any('write-tree' in argv or 'commit' in argv for argv in calls)


def test_final_guard_crossing_deadline_cannot_return_delivery(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    original_guard = execution._guard_workspace
    original_monotonic = execution.time.monotonic
    offset = [0]
    monkeypatch.setattr(execution, 'time', SimpleNamespace(monotonic=lambda: original_monotonic() + offset[0]))
    def slow_final_guard(root, before, changed):
        original_guard(root, before, changed)
        if root.name == 'integration':
            offset[0] = 120
    monkeypatch.setattr(execution, '_guard_workspace', slow_final_guard)
    cancel = threading.Event()
    with pytest.raises(execution.ExecutionError, match='execution timeout') as caught:
        execution.execute_plan(run_id='final-deadline', plan={'tasks': [_task('first')]},
            project={**_project(repo), 'checks': {'first': ['true']}, 'unknown_cost_policy': 'allow_bounded'},
            profiles=_profiles(), runner=ExecutionRunner({'first': None}), emit=lambda *_: None,
            cancel=cancel, timeout_s=30)
    assert cancel.is_set()
    assert Path(caught.value.artifacts['worktree']).is_dir()
    assert caught.value.artifacts['tasks'][0]['status'] == 'verified'


@pytest.mark.parametrize('cost', [float('nan'), float('inf'), float('-inf'), -1.0, 'bad-price', True])
@pytest.mark.parametrize('policy', ['stop', 'allow_bounded'])
def test_invalid_cost_is_unknown_and_blocks_automatic_publication(tmp_path, cost, policy):
    repo = _git_repo(tmp_path)
    runner = ExecutionRunner({'first': cost, 'second': 0.25})
    arguments = dict(run_id='invalid-cost',
        plan={'tasks': [_task('first'), _task('second', depends_on=['first'])]},
        project={**_project(repo), 'unknown_cost_policy': policy}, profiles=_profiles(),
        runner=runner, emit=lambda *_: None, cancel=threading.Event())
    if policy == 'stop':
        with pytest.raises(execution.ExecutionError) as caught:
            execution.execute_plan(**arguments)
        artifacts = caught.value.artifacts
        assert len(runner.seen) == 1
    else:
        artifacts = execution.execute_plan(**arguments)
        assert len(runner.seen) == 2
    assert artifacts['observed_cost_usd'] is None
    assert artifacts['billing_incomplete']
    assert artifacts['autopublish_blocked'] is True
    assert math.isfinite(artifacts['known_cost_usd'])
    assert artifacts['tasks'][0]['cost_usd'] is None
    json.dumps(artifacts, allow_nan=False)


def test_cost_subtotal_overflow_fails_without_nonfinite_json(tmp_path):
    repo = _git_repo(tmp_path)
    runner = ExecutionRunner({'first': 1e308, 'second': 1e308})
    with pytest.raises(execution.ExecutionError, match='subtotal overflowed') as caught:
        execution.execute_plan(run_id='cost-overflow',
            plan={'tasks': [_task('first'), _task('second')]}, project=_project(repo),
            profiles=_profiles(), runner=runner, emit=lambda *_: None, cancel=threading.Event())
    artifacts = caught.value.artifacts
    assert artifacts['observed_cost_usd'] is None
    assert artifacts['autopublish_blocked']
    assert math.isfinite(artifacts['known_cost_usd'])
    json.dumps(artifacts, allow_nan=False)
