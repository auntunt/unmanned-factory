"""Observed terminal results survive execution as bounded verification evidence."""
import threading
from pathlib import Path

import pytest

from factory.control import claude_terminal
from factory.control.execution import execute_plan
from tests.test_control_execution import repo, provider_module, _project, Result


@pytest.mark.parametrize('exit_code,timeout', [(0, False), (1, False), (-9, True)])
def test_terminal_emits_result_without_turning_failure_into_success(monkeypatch, tmp_path, exit_code, timeout):
    monkeypatch.setenv('TEST_API_KEY', 'private-test-credential')
    monkeypatch.setattr(claude_terminal, '_run_command_unlimited', lambda *args: {
        'exit_code': exit_code, 'timeout': timeout, 'output': 'x' * 5000 + 'private-test-credential'})
    events = []
    result = claude_terminal.run_command(tmp_path, 'python -m unittest', emit=lambda *event: events.append(event))
    evidence = next(payload for kind, payload in events if kind == 'command.completed')
    assert evidence['exit_code'] == result['exit_code'] == exit_code
    assert evidence['timeout'] is timeout
    assert evidence['truncated'] is True
    assert 'private-test-credential' not in evidence['output']
    assert len(evidence['output']) <= 4000
    assert events[-1] == ('task.activity', {'phase': 'model'})


def test_terminal_denial_is_recorded_as_failure(tmp_path):
    events = []
    claude_terminal.run_command(tmp_path, 'git reset --hard', emit=lambda *event: events.append(event))
    evidence = next(payload for kind, payload in events if kind == 'command.completed')
    assert evidence['exit_code'] is None
    assert evidence['error']


def test_command_evidence_survives_verified_commit(repo, provider_module):
    class Runner:
        def run(self, request, emit, cancel=None):
            Path(request.workspace, 'answer.txt').write_text('42\n')
            for index in range(25):
                emit('command.completed', {'source': 'isolated_project_terminal',
                    'command': f'check-{index}', 'exit_code': 1 if index == 24 else 0,
                    'output': 'observed', 'timeout': False})
            return Result()
    artifacts = execute_plan(run_id='evidence', plan={'tasks': [
        {'id': 'answer', 'prompt': 'answer', 'paths': ['answer.txt'], 'checks': ['ok']}]
    }, project=_project(repo), profiles={'standard': {'provider': 'fake', 'model': 'test'}},
        runner=Runner(), emit=lambda *_: None, cancel=threading.Event())
    task = artifacts['tasks'][0]
    assert task['status'] == 'verified'  # trusted check, not arbitrary command exit
    assert len(task['command_evidence']) == 20
    assert task['command_evidence'][0]['command'] == 'check-5'
    assert task['command_evidence'][-1]['exit_code'] == 1
    assert task['attempts'][0]['command_evidence'] == task['command_evidence']
    assert (Path(artifacts['worktree']) / 'answer.txt').read_text() == '42\n'
