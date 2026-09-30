"""A process exit during checks must not discard completed coding or evidence."""
import json
import sys
from pathlib import Path

import pytest

from factory.control import continuous
from factory.control.execution import ExecutionError
from factory.control.recovery import _continuous_resume_stage
from tests.test_continuous_execution import Writer, repo, run


def interrupted_checks(repo, tmp_path, monkeypatch, *, crash_at='second'):
    counters = {name: tmp_path / (name + '.count') for name in ('first', 'second')}
    project = {**repo, 'checks': {name: [sys.executable, '-c',
        "from pathlib import Path; import main; assert main.double(7) == 14; "
        f"p=Path({str(path)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
        for name, path in counters.items()}}
    original = continuous._run_check
    snapshots = []
    runner = Writer()

    def crash(root, name, *args, **kwargs):
        if name == crash_at:
            raise SystemExit('coordinator exited during checks')
        return original(root, name, *args, **kwargs)

    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            snapshots.append(json.loads(json.dumps(payload['continuous_artifacts'])))

    monkeypatch.setattr(continuous, '_run_check', crash)
    with pytest.raises(SystemExit):
        run(project, runner, emit=emit)
    monkeypatch.setattr(continuous, '_run_check', original)
    return project, runner, snapshots[-1], counters


@pytest.mark.parametrize('crash_at', ['first', 'second'])
def test_restart_runs_only_missing_checks_without_recoding(repo, tmp_path, monkeypatch, crash_at):
    project, runner, saved, counters = interrupted_checks(repo, tmp_path, monkeypatch, crash_at=crash_at)
    assert _continuous_resume_stage(saved) == 'checks'
    result = run(project, runner, resume_artifacts=saved,
                 task_fields={'resume_stage': 'checks'})
    assert len(runner.requests) == 1
    assert all(path.read_text() == 'x' for path in counters.values())
    assert result['tasks'][0]['status'] == 'verified'
    assert result['commit']
    assert 'checks_checkpoint' not in result


def test_partial_check_recovery_rejects_changed_source(repo, tmp_path, monkeypatch):
    project, runner, saved, _ = interrupted_checks(repo, tmp_path, monkeypatch)
    Path(saved['worktree'], 'main.py').write_text('def double(n): return 0\n')
    with pytest.raises(ExecutionError, match='source changed'):
        run(project, runner, resume_artifacts=saved, task_fields={'resume_stage': 'checks'})
    assert len(runner.requests) == 1


def test_partial_recovery_reruns_changed_check_identity(repo, tmp_path, monkeypatch):
    project, runner, saved, counters = interrupted_checks(repo, tmp_path, monkeypatch)
    project['checks']['first'] = [*project['checks']['first'], '--new-option']
    result = run(project, runner, resume_artifacts=saved, task_fields={'resume_stage': 'checks'})
    assert len(runner.requests) == 1
    assert counters['first'].read_text() == 'xx'
    assert counters['second'].read_text() == 'x'
    assert result['commit']


def test_failed_partial_checkpoint_requires_coding_recovery():
    assert _continuous_resume_stage({'checks_checkpoint': {
        'checks': [{'name': 'first', 'exit': 1}]}}) is None


def test_passing_check_that_mutates_source_is_not_saved_as_reusable(repo):
    project = {**repo, 'checks': {'mutator': [sys.executable, '-c',
        "from pathlib import Path; Path('main.py').write_text('def double(n): return 0\\n')"]}}
    snapshots = []
    def emit(kind, payload, *_):
        if kind == 'execution.checkpoint':
            snapshots.append(json.loads(json.dumps(payload['continuous_artifacts'])))
    with pytest.raises(ExecutionError, match='verification changed source files'):
        run(project, Writer(), emit=emit)
    assert snapshots[-1]['checks_checkpoint']['checks'] == []
    with pytest.raises(ExecutionError, match='source changed'):
        run(project, Writer(), resume_artifacts=snapshots[-1],
            task_fields={'resume_stage': 'checks'})


def test_missing_partial_checkpoint_does_not_fall_back_to_coding(repo):
    runner = Writer()
    saved = run(repo, runner)
    with pytest.raises(ExecutionError, match='checks checkpoint missing'):
        run(repo, runner, resume_artifacts=saved, task_fields={'resume_stage': 'checks'})
    assert len(runner.requests) == 1


def test_partial_recovery_reruns_pass_after_execution_environment_changes(repo, tmp_path, monkeypatch):
    monkeypatch.setenv('CI', '0')
    project, runner, saved, counters = interrupted_checks(repo, tmp_path, monkeypatch)
    monkeypatch.setenv('CI', '1')
    result = run(project, runner, resume_artifacts=saved, task_fields={'resume_stage': 'checks'})
    assert len(runner.requests) == 1
    assert counters['first'].read_text() == 'xx'
    assert counters['second'].read_text() == 'x'
    assert result['commit']


def test_partial_recovery_reruns_pass_after_requirement_identity_changes(repo, tmp_path, monkeypatch):
    project, runner, saved, counters = interrupted_checks(repo, tmp_path, monkeypatch)
    result = run(project, runner, resume_artifacts=saved, task_fields={
        'resume_stage': 'checks', 'effective_revision': 2, 'effective_digest': 'new-requirement'})
    assert len(runner.requests) == 1
    assert counters['first'].read_text() == 'xx'
    assert counters['second'].read_text() == 'x'
    assert result['commit']
