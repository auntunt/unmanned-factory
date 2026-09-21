"""A recorded check result is evidence only for what it actually ran against.

Reuse used to be all-or-nothing on the source hash: any edited check discarded
every other check's valid result, while the command itself, the tool and
environment that ran it, and the requirement it was judged against were not part
of the decision at all. Same argv under a different interpreter, or an old pass
carried across a supplement, both read as "already verified".

Every case below runs a real local check in a real git worktree and counts actual
executions, because a model or a helper saying "reused" proves nothing.
"""
import copy
import os
import subprocess
import threading
from pathlib import Path

import pytest

from factory.control import evidence_identity
from factory.control.continuous import execute_continuous
from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult


def _repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-b', 'main'], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'f@l'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'F'], cwd=root, check=True)
    (root / 'app.py').write_text("VALUE = 1\n")
    # The check counts its own runs, in a file outside the worktree diff.
    counter = tmp_path / 'runs.txt'
    (root / 'check.sh').write_text(
        f'#!/bin/sh\necho ran >> {counter}\nexit 0\n')
    os.chmod(root / 'check.sh', 0o755)
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-m', 'base'], cwd=root, check=True,
                   capture_output=True)
    return root, counter


def _runs(counter):
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _project(root, *, checks=None):
    return {'id': 'p1', 'workspace': str(root), 'base_branch': 'main',
            'checks': checks or {'local': ['./check.sh']}, 'budget_usd': 5.0,
            'max_tasks': 1}


def _plan(**task):
    return {'tasks': [{'id': 't1', 'title': 'work', 'prompt': 'do it',
                       **task}]}


PROFILES = {'standard': {'provider': 'claude', 'model': 'coder'}}


def _coder(text='done'):
    def run(request, emit, cancel=None):
        root = Path(request.workspace)
        (root / 'app.py').write_text('VALUE = 2\n')
        return ProviderResult(text, cost_usd=0.01)
    return run


class _Runner:
    def __init__(self, fn):
        self.run = fn


def _execute(project, plan, runner, **kw):
    events = []

    def emit(kind, payload, task_id=None):
        # Snapshot: the executor keeps mutating the same artifacts dict, and a
        # checkpoint has to mean what the site looked like at that moment.
        events.append((kind, copy.deepcopy(payload)))

    artifacts = execute_continuous(
        run_id='r1', plan=plan, project=project, profiles=PROFILES, runner=runner,
        emit=emit, cancel=threading.Event(), timeout_s=120, **kw)
    return artifacts, events


def _checkpoint_before_commit(events):
    """The durable checkpoint a crash between checks and commit would leave."""
    return next(payload['continuous_artifacts'] for kind, payload in reversed(events)
                if kind == 'execution.checkpoint'
                and payload['continuous_artifacts'].get('finalization_checkpoint'))


def _reused_checks(events):
    return [payload['check'] for kind, payload in events
            if kind == 'execution.reused' and payload.get('stage') == 'check']


def _crash_before_commit(checkpoint):
    """Put the worktree back where a crash between checks and commit leaves it.

    The checkpoint was written before `finalize`, so its own guard describes an
    uncommitted tree. The test run went on to commit; undoing that commit while
    keeping the working tree is what the recovery path is actually built for.
    """
    worktree = Path(checkpoint['worktree'])
    subprocess.run(['git', 'reset', '--mixed', checkpoint['base_sha']], cwd=worktree,
                   check=True, capture_output=True)
    return checkpoint


def _resume(project, checkpoint, runner, *, plan_task=None):
    """Re-enter the site at the finalization stage, the way recovery does."""
    return _execute(project, _plan(resume_stage='finalization', **(plan_task or {})),
                    runner, resume_artifacts=_crash_before_commit(checkpoint))


def test_a_real_local_check_is_reused_when_its_whole_identity_is_unchanged(tmp_path):
    """One real check, run once, honoured across a finalization recovery."""
    root, counter = _repo(tmp_path)
    project = _project(root)
    artifacts, events = _execute(project, _plan(), _Runner(_coder()))
    assert _runs(counter) == 1
    saved = artifacts['checks'][0]
    assert saved['exit'] == 0 and saved['identity_fingerprint']
    assert _reused_checks(events) == []

    checkpoint = _checkpoint_before_commit(events)
    resumed, resumed_events = _resume(project, checkpoint, _Runner(
        lambda *a, **k: pytest.fail('finalization recovery dispatched a coding call')))
    assert _runs(counter) == 1, 'a valid result was re-earned instead of reused'
    assert _reused_checks(resumed_events) == ['local']
    assert resumed['commit']
    assert resumed['checks'][0]['reused'] is True


def test_editing_the_check_command_makes_its_old_result_stop_counting(tmp_path):
    """Same source, different argv: the saved pass is not evidence for it."""
    root, counter = _repo(tmp_path)
    artifacts, events = _execute(_project(root), _plan(), _Runner(_coder()))
    assert _runs(counter) == 1
    checkpoint = _checkpoint_before_commit(events)

    edited = _project(root, checks={'local': ['./check.sh', '--strict']})
    resumed, resumed_events = _resume(edited, checkpoint, _Runner(
        lambda *a, **k: pytest.fail('a changed check triggered a coding call')))
    assert _reused_checks(resumed_events) == []
    assert _runs(counter) == 2, 'the edited check was not re-run'
    assert resumed['commit']


def test_one_edited_check_does_not_discard_another_checks_valid_result(tmp_path):
    """Invalidation is scoped to what changed, not to the whole recovery."""
    root, counter = _repo(tmp_path)
    second = tmp_path / 'second.txt'
    (root / 'other.sh').write_text(f'#!/bin/sh\necho ran >> {second}\nexit 0\n')
    os.chmod(root / 'other.sh', 0o755)
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'second check'], cwd=root, check=True,
                   capture_output=True)
    project = _project(root, checks={'local': ['./check.sh'], 'other': ['./other.sh']})
    artifacts, events = _execute(project, _plan(), _Runner(_coder()))
    assert _runs(counter) == 1 and _runs(second) == 1
    checkpoint = _checkpoint_before_commit(events)

    edited = _project(root, checks={'local': ['./check.sh'],
                                    'other': ['./other.sh', '--more']})
    resumed, resumed_events = _resume(edited, checkpoint, _Runner(
        lambda *a, **k: pytest.fail('recovery dispatched a coding call')))
    assert _reused_checks(resumed_events) == ['local']
    assert _runs(counter) == 1, 'an unchanged check lost its valid result'
    assert _runs(second) == 2, 'the edited check was not re-run'
    assert resumed['commit']


def test_a_changed_tool_environment_makes_the_same_argv_a_different_check(tmp_path,
                                                                         monkeypatch):
    """Same command, different executable behind it: not the same evidence.

    This is the direction that reads most like success -- the argv is identical, the
    source is identical -- and it is where a green result survives the change that
    would have broken it.
    """
    root, counter = _repo(tmp_path)
    project = _project(root, checks={'local': ['countcheck']})
    first_bin = tmp_path / 'bin1'
    first_bin.mkdir()
    (first_bin / 'countcheck').write_text(f'#!/bin/sh\necho ran >> {counter}\nexit 0\n')
    os.chmod(first_bin / 'countcheck', 0o755)
    monkeypatch.setenv('PATH', f'{first_bin}:{os.environ["PATH"]}')
    artifacts, events = _execute(project, _plan(), _Runner(_coder()))
    assert _runs(counter) == 1
    checkpoint = _checkpoint_before_commit(events)

    # A different directory earlier on PATH: `countcheck` now means another file.
    second_bin = tmp_path / 'bin2'
    second_bin.mkdir()
    (second_bin / 'countcheck').write_text(f'#!/bin/sh\necho ran >> {counter}\nexit 0\n')
    os.chmod(second_bin / 'countcheck', 0o755)
    monkeypatch.setenv('PATH', f'{second_bin}:{os.environ["PATH"]}')
    resumed, resumed_events = _resume(project, checkpoint, _Runner(
        lambda *a, **k: pytest.fail('recovery dispatched a coding call')))
    assert _reused_checks(resumed_events) == []
    assert _runs(counter) == 2, 'the check was reused across a changed tool identity'
    assert resumed['commit']


def test_a_requirement_change_forces_coverage_to_be_established_again(tmp_path):
    """An old pass is not relabelled under the revision it never covered."""
    root, counter = _repo(tmp_path)
    project = _project(root)
    artifacts, events = _execute(
        project, _plan(effective_revision=2, effective_digest='digest-a'),
        _Runner(_coder()))
    assert _runs(counter) == 1
    checkpoint = _checkpoint_before_commit(events)

    # A supplement revised the agreement: same code, same command, same tools.
    resumed, resumed_events = _resume(
        project, checkpoint, _Runner(lambda *a, **k: pytest.fail('coding call')),
        plan_task={'effective_revision': 3, 'effective_digest': 'digest-b'})
    assert _reused_checks(resumed_events) == []
    assert _runs(counter) == 2, 'coverage was carried across a revised requirement'
    assert resumed['commit']


def test_an_unrecorded_identity_is_unverified_rather_than_assumed_passing(tmp_path):
    """A result with no identity -- a legacy checkpoint -- is re-earned, not trusted."""
    root, counter = _repo(tmp_path)
    project = _project(root)
    artifacts, events = _execute(project, _plan(), _Runner(_coder()))
    assert _runs(counter) == 1
    checkpoint = _checkpoint_before_commit(events)
    for record in checkpoint['finalization_checkpoint']['checks']:
        record.pop('identity_fingerprint', None)   # written before identities existed

    resumed, resumed_events = _resume(project, checkpoint, _Runner(
        lambda *a, **k: pytest.fail('recovery dispatched a coding call')))
    assert _reused_checks(resumed_events) == []
    assert _runs(counter) == 2, 'an identity-less result was treated as verified'
    assert resumed['commit']


def test_a_deployment_health_observation_expires(tmp_path):
    """Health is a fact about now; an old 200 is a record of the past.

    Absence of a timestamp does not pass, and neither does a time in the future --
    a clock skew must not extend the window it cannot be checked against.
    """
    assert evidence_identity.health_is_current('2026-09-21T10:00:00+00:00',
        ttl_s=900, at='2026-09-21T10:10:00+00:00') is True
    assert evidence_identity.health_is_current('2026-09-21T10:00:00+00:00',
        ttl_s=900, at='2026-09-21T10:20:00+00:00') is False
    assert evidence_identity.health_is_current(None, at='2026-09-21T10:00:00+00:00') is False
    assert evidence_identity.health_is_current('not-a-time',
        at='2026-09-21T10:00:00+00:00') is False
    assert evidence_identity.health_is_current('2026-09-21T11:00:00+00:00',
        ttl_s=900, at='2026-09-21T10:00:00+00:00') is False


def test_the_same_executable_under_a_changed_import_path_is_not_the_same_check(
        tmp_path, monkeypatch):
    """`PYTHONPATH` decides what the check imports, so it decides what it proves.

    Here the argv, the resolved executable and its bytes are all identical -- the
    only thing that moved is where its imports come from. That is exactly the
    substitution a resolved-path fingerprint cannot see.
    """
    root, counter = _repo(tmp_path)
    (root / 'check.sh').write_text(
        f'#!/bin/sh\necho ran >> {counter}\npython -c "import dep" || exit 1\n')
    os.chmod(root / 'check.sh', 0o755)
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'import check'], cwd=root, check=True,
                   capture_output=True)
    first = tmp_path / 'libs1'
    first.mkdir()
    (first / 'dep.py').write_text('VALUE = 1\n')
    monkeypatch.setenv('PYTHONPATH', str(first))
    project = _project(root)
    artifacts, events = _execute(project, _plan(), _Runner(_coder()))
    assert _runs(counter) == 1
    checkpoint = _checkpoint_before_commit(events)

    second = tmp_path / 'libs2'
    second.mkdir()
    (second / 'dep.py').write_text('VALUE = 2\n')
    monkeypatch.setenv('PYTHONPATH', str(second))
    resumed, resumed_events = _resume(project, checkpoint, _Runner(
        lambda *a, **k: pytest.fail('recovery dispatched a coding call')))
    assert _reused_checks(resumed_events) == []
    assert _runs(counter) == 2, 'reused across a changed import environment'
    assert resumed['commit']
