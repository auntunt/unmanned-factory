"""A durable exact commit intent permits narrow, idempotent crash recovery."""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from factory.control import continuous
from factory.control.execution import ExecutionError
from tests.test_continuous_execution import Writer, repo, run


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def crash_after_commit(repo, monkeypatch):
    original = continuous._commit_tree
    snapshots = []
    runner = Writer()
    def commit_then_exit(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit('exit after ref update, before completion checkpoint')
    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            snapshots.append(json.loads(json.dumps(payload['continuous_artifacts'])))
    monkeypatch.setattr(continuous, '_commit_tree', commit_then_exit)
    with pytest.raises(SystemExit):
        run(repo, runner, emit=emit)
    monkeypatch.setattr(continuous, '_commit_tree', original)
    return runner, snapshots[-1]


def test_exact_committed_intent_recovers_without_recoding_or_rechecking(repo, monkeypatch):
    runner, saved = crash_after_commit(repo, monkeypatch)
    committed = git(saved['worktree'], 'rev-parse', 'HEAD')
    events = []
    result = run(repo, runner, resume_artifacts=saved,
        task_fields={'resume_stage': 'finalization'}, emit=lambda *event: events.append(event))
    assert result['commit'] == committed
    assert len(runner.requests) == 1
    assert not any(event[0] == 'check.result' for event in events)
    assert result['tasks'][0]['status'] == 'verified'
    assert 'commit_intent' not in result
    # The same durable pre-crash intent can be replayed again without another commit.
    again = run(repo, runner, resume_artifacts=saved, task_fields={'resume_stage': 'finalization'})
    assert again['commit'] == committed
    assert len(runner.requests) == 1


@pytest.mark.parametrize('change', ['different_commit', 'extra_commit', 'dirty', 'branch',
                                   'config', 'missing_intent', 'version', 'parent', 'tree',
                                   'missing_checks', 'failed_check', 'code_signature'])
def test_reconciliation_rejects_unattributable_or_mutated_state(repo, monkeypatch, change):
    runner, saved = crash_after_commit(repo, monkeypatch)
    root = saved['worktree']
    intent = saved['commit_intent']
    if change in ('different_commit', 'extra_commit'):
        parent = intent['parent'] if change == 'different_commit' else intent['expected_commit']
        other = git(root, 'commit-tree', intent['tree'], '-p', parent, '-m', 'not the intended commit')
        git(root, 'update-ref', intent['branch'], other)
    elif change == 'dirty':
        Path(root, 'main.py').write_text('def double(n): return 0\n')
    elif change == 'branch':
        git(root, 'checkout', '-b', 'unrelated-branch')
    elif change == 'config':
        git(root, 'config', 'core.filemode', 'false')
    elif change == 'missing_intent':
        saved.pop('commit_intent')
    elif change == 'version':
        intent['version'] = True
    elif change == 'parent':
        intent['parent'] = intent['expected_commit']
    elif change == 'tree':
        intent['tree'] = git(root, 'rev-parse', intent['parent'] + '^{tree}')
    elif change == 'missing_checks':
        saved['finalization_checkpoint']['checks'] = []
    elif change == 'failed_check':
        saved['finalization_checkpoint']['checks'][0]['exit'] = 1
    elif change == 'code_signature':
        saved['checks_identity_code']['signature'] = 'unrelated-code'
    with pytest.raises(ExecutionError, match='metadata changed|branch mismatch'):
        run(repo, runner, resume_artifacts=saved, task_fields={'resume_stage': 'finalization'})
    assert len(runner.requests) == 1


def test_commit_recovery_revalidates_environment_before_reusing_checks(repo, monkeypatch):
    monkeypatch.setenv('CI', '0')
    runner, saved = crash_after_commit(repo, monkeypatch)
    monkeypatch.setenv('CI', '1')
    events = []
    result = run(repo, runner, resume_artifacts=saved,
        task_fields={'resume_stage': 'finalization'}, emit=lambda *event: events.append(event))
    assert any(event[0] == 'check.result' for event in events)
    assert len(runner.requests) == 1
    assert result['commit'] == saved['commit_intent']['expected_commit']


def test_crash_after_intent_before_ref_update_is_recoverable(repo, monkeypatch):
    from factory.control import execution
    original = execution._git_ok
    snapshots = []
    runner = Writer()
    def crash(root, *args, **kwargs):
        if args[0] == 'update-ref':
            raise SystemExit('intent saved, ref not updated')
        return original(root, *args, **kwargs)
    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            snapshots.append(copy.deepcopy(payload['continuous_artifacts']))
    monkeypatch.setattr(execution, '_git_ok', crash)
    with pytest.raises(SystemExit):
        run(repo, runner, emit=emit)
    saved = snapshots[-1]
    assert git(saved['worktree'], 'rev-parse', 'HEAD') == saved['commit_intent']['parent']
    monkeypatch.setattr(execution, '_git_ok', original)
    result = run(repo, runner, resume_artifacts=saved, task_fields={'resume_stage': 'finalization'})
    assert result['tasks'][0]['status'] == 'verified'
    assert len(runner.requests) == 1


def test_failed_intent_persistence_does_not_advance_branch(repo):
    snapshots = []
    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            saved = copy.deepcopy(payload['continuous_artifacts'])
            snapshots.append(saved)
            if saved.get('commit_intent'):
                raise RuntimeError('database unavailable')
    with pytest.raises(ExecutionError, match='event emission failed'):
        run(repo, Writer(), emit=emit)
    saved = snapshots[-1]
    assert git(saved['worktree'], 'rev-parse', 'HEAD') == saved['base_sha']
    assert Path(saved['worktree'], 'main.py').exists()


def test_signing_policy_cannot_silently_produce_unsigned_commit(repo):
    git(repo['workspace'], 'config', 'commit.gpgSign', 'true')
    git(repo['workspace'], 'config', 'gpg.program', '/bin/false')
    with pytest.raises(ExecutionError, match='sign|commit-tree') as caught:
        run(repo, Writer())
    saved = caught.value.artifacts
    assert git(saved['worktree'], 'rev-parse', 'HEAD') == saved['base_sha']
    assert saved.get('commit') is None


def test_cancellation_after_ref_update_restores_uncommitted_work(repo, monkeypatch):
    import threading
    from factory.control import execution
    original = execution._git_ok
    cancel = threading.Event()
    def cancel_after_update(root, *args, **kwargs):
        result = original(root, *args, **kwargs)
        if args[0] == 'update-ref':
            cancel.set()
        return result
    monkeypatch.setattr(execution, '_git_ok', cancel_after_update)
    with pytest.raises(ExecutionError, match='execution cancelled') as caught:
        run(repo, Writer(), cancel=cancel)
    saved = caught.value.artifacts
    assert git(saved['worktree'], 'rev-parse', 'HEAD') == saved['base_sha']
    assert Path(saved['worktree'], 'main.py').exists()
    assert 'commit_intent' not in saved


def test_cas_does_not_overwrite_or_rollback_an_external_commit(repo, monkeypatch):
    from factory.control import execution
    original = execution._git_ok
    external = []
    def intervene(root, *args, **kwargs):
        if args[0] == 'update-ref':
            current = git(root, 'rev-parse', 'HEAD')
            tree = git(root, 'rev-parse', 'HEAD^{tree}')
            other = git(root, 'commit-tree', tree, '-p', current, '-m', 'external writer')
            git(root, 'update-ref', 'HEAD', other)
            external.append(other)
        return original(root, *args, **kwargs)
    monkeypatch.setattr(execution, '_git_ok', intervene)
    with pytest.raises(ExecutionError, match='refusing to roll back') as caught:
        run(repo, Writer())
    assert git(caught.value.artifacts['worktree'], 'rev-parse', 'HEAD') == external[0]


def test_second_crash_during_reconciliation_remains_recoverable(repo, monkeypatch):
    monkeypatch.setenv('CI', '0')
    runner, saved = crash_after_commit(repo, monkeypatch)
    monkeypatch.setenv('CI', '1')
    original = continuous._run_check
    snapshots = []
    def crash(*args, **kwargs):
        raise SystemExit('second crash during check revalidation')
    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            snapshots.append(copy.deepcopy(payload['continuous_artifacts']))
    monkeypatch.setattr(continuous, '_run_check', crash)
    with pytest.raises(SystemExit):
        run(repo, runner, resume_artifacts=saved,
            task_fields={'resume_stage': 'finalization'}, emit=emit)
    monkeypatch.setattr(continuous, '_run_check', original)
    result = run(repo, runner, resume_artifacts=snapshots[-1],
                 task_fields={'resume_stage': 'finalization'})
    assert result['commit'] == saved['commit_intent']['expected_commit']
    assert len(runner.requests) == 1


def test_new_feedback_after_commit_crash_still_reaches_coding_model(repo, monkeypatch):
    runner, saved = crash_after_commit(repo, monkeypatch)
    result = run(repo, runner, resume_artifacts=saved,
        task_fields={'resume_stage': None, 'resume_feedback': 'Add the requested export feature'})
    assert len(runner.requests) == 2
    assert 'Add the requested export feature' in runner.requests[-1].prompt
    assert result['commit'] == saved['commit_intent']['expected_commit']


def test_preintent_staging_crash_still_fails_closed_without_claiming_recovery(repo, monkeypatch):
    from factory.control import execution
    original = execution._git_ok
    snapshots = []
    runner = Writer()
    def crash(root, *args, **kwargs):
        if 'commit-tree' in args:
            raise SystemExit('staged, but no durable intent yet')
        return original(root, *args, **kwargs)
    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            snapshots.append(copy.deepcopy(payload['continuous_artifacts']))
    monkeypatch.setattr(execution, '_git_ok', crash)
    with pytest.raises(SystemExit):
        run(repo, runner, emit=emit)
    monkeypatch.setattr(execution, '_git_ok', original)
    saved = snapshots[-1]
    assert not saved.get('commit_intent')
    assert git(saved['worktree'], 'rev-parse', 'HEAD') == saved['base_sha']
    with pytest.raises(ExecutionError, match='source changed after checks'):
        run(repo, runner, resume_artifacts=saved, task_fields={'resume_stage': 'finalization'})
    assert len(runner.requests) == 1


def test_branch_switch_at_ref_update_is_not_reported_as_completed(repo, monkeypatch):
    from factory.control import execution
    original = execution._git_ok
    def intervene(root, *args, **kwargs):
        if args[0] == 'update-ref':
            git(root, 'branch', 'external-branch')
            git(root, 'symbolic-ref', 'HEAD', 'refs/heads/external-branch')
        return original(root, *args, **kwargs)
    monkeypatch.setattr(execution, '_git_ok', intervene)
    with pytest.raises(ExecutionError, match='refusing to roll back') as caught:
        run(repo, Writer())
    assert git(caught.value.artifacts['worktree'], 'symbolic-ref', 'HEAD') == 'refs/heads/external-branch'
    assert caught.value.artifacts.get('commit') is None
