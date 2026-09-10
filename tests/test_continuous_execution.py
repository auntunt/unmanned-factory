from pathlib import Path
import json
import subprocess
import sys
import threading

import pytest

from factory.control.continuous import execute_continuous
from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult, ProviderError, ProviderCancelled


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    for args in (('init', '-q'), ('config', 'user.name', 'test'),
                 ('config', 'user.email', 'test@example.com')):
        subprocess.run(['git', *args], cwd=root, check=True)
    (root / 'README.md').write_text('CLI project\n')
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, check=True)
    branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=root, text=True).strip()
    return {'workspace': str(root), 'base_branch': branch, 'checks': {
        'behavior': [sys.executable, '-c', "import main; assert main.double(7) == 14"]}}


def run(repo, runner, **kwargs):
    return execute_continuous(run_id=kwargs.pop('run_id', 'continuous'),
        plan={'tasks': [{'id': 'coding', 'title': 'Double a number', 'prompt': 'Build double CLI',
                         'complexity': 'medium', 'risk': 'low', 'paths': ['src'], 'checks': ['behavior'], **kwargs.pop('task_fields', {})}]},
        project=repo, profiles={'standard': {'provider': 'claude', 'model': 'model'}},
        runner=runner, cancel=kwargs.pop('cancel', threading.Event()),
        emit=kwargs.pop('emit', lambda *_: None), **kwargs)


class Writer:
    def __init__(self):
        self.requests = []

    def run(self, request, emit, cancel=None):
        self.requests.append(request)
        root = Path(request.workspace)
        (root / 'main.py').write_text('def double(n): return n * 2\n')
        # Ordinary test/build config must not become a new approval gate.
        (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
        emit('provider.session', {'session_id': 'session-1'})
        return ProviderResult(text='Implemented double; behavior check available.', session_id='session-1', cost_usd=0.1)


def test_one_workspace_one_final_check_and_real_behavior(repo):
    events = []
    runner = Writer()
    result = run(repo, runner, emit=lambda *event: events.append(json.loads(json.dumps(event))))
    assert len(runner.requests) == 1
    assert result['execution_mode'] == 'continuous'
    assert result['tasks'][0]['status'] == 'verified'
    assert result['session_id'] == 'session-1'
    assert result['checks'][0]['exit'] == 0
    assert len([e for e in events if e[0] == 'check.result']) == 1
    assert not Path(repo['workspace'], 'main.py').exists()
    assert subprocess.check_output([sys.executable, '-c', 'import main; print(main.double(9))'],
                                   cwd=result['worktree'], text=True).strip() == '18'
    assert result['known_cost_usd'] == 0.1


def test_gateway_governed_passthrough_keeps_usage(repo):
    from factory.control.governance import GovernedRunner
    result = run(repo, GovernedRunner(Writer(), object()))
    assert result['known_cost_usd'] == 0.1


def test_transient_failure_reuses_session_workspace_and_dependencies(repo, monkeypatch):
    monkeypatch.setattr(threading.Event, 'wait', lambda self, timeout=None: self.is_set())
    class Flaky(Writer):
        def run(self, request, emit, cancel=None):
            if not self.requests:
                self.requests.append(request)
                root = Path(request.workspace)
                (root / '.venv').mkdir()
                (root / '.venv' / 'marker').write_text('installed')
                (root / 'main.py').write_text('draft')
                emit('provider.session', {'session_id': 'session-1'})
                raise ProviderError('API Error: 504')
            assert request.session_id == 'session-1'
            assert request.workspace == self.requests[0].workspace
            assert Path(request.workspace, '.venv/marker').read_text() == 'installed'
            assert Path(request.workspace, 'main.py').read_text() == 'draft'
            return super().run(request, emit, cancel)
    runner = Flaky()
    events = []
    result = run(repo, runner, emit=lambda *e: events.append(e))
    assert len(runner.requests) == 2
    assert any(e[0] == 'execution.reconnecting' for e in events)
    assert len(result['tasks'][0]['attempts']) == 2


def test_final_check_failure_repairs_in_same_session(repo):
    class Repair(Writer):
        def run(self, request, emit, cancel=None):
            if not self.requests:
                self.requests.append(request)
                Path(request.workspace, 'main.py').write_text('def double(n): return n + 2  # incorrect first draft\n')
                return ProviderResult(text='first draft', session_id='session-1', cost_usd=0.1)
            assert 'repair the actual failing check' in request.prompt
            assert request.session_id == 'session-1'
            assert request.workspace == self.requests[0].workspace
            return super().run(request, emit, cancel)
    runner = Repair()
    result = run(repo, runner)
    assert len(runner.requests) == 2
    assert result['tasks'][0]['attempts'][0]['checks'][0]['exit'] != 0
    assert result['checks'][0]['exit'] == 0


def test_cancelled_execution_recovers_same_directory_and_session(repo):
    class Interrupted(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n * 2\n')
            emit('provider.session', {'session_id': 'session-1'})
            cancel.set()
            raise ProviderCancelled('cancelled')
    with pytest.raises(ExecutionError) as stopped:
        run(repo, Interrupted())
    artifact = json.loads(json.dumps(stopped.value.artifacts))
    assert artifact['tasks'][0]['status'] == 'cancelled'
    runner = Writer()
    resumed = run(repo, runner, run_id='resumed', resume_artifacts=artifact)
    assert resumed['worktree'] == artifact['worktree']
    assert runner.requests[0].session_id == 'session-1'
    assert len(resumed['tasks'][0]['attempts']) == 2


def test_review_repair_preserves_commit_and_single_workspace(repo):
    first = run(repo, Writer())
    first['verification'] = {'verdict': 'fail', 'reason': 'need another example'}
    first['verification_repair_count'] = 1
    again = run(repo, Writer(), resume_artifacts=first)
    assert again['worktree'] == first['worktree']
    assert again['verification_repair_count'] == 1
    assert 'verification' not in again


def test_recovery_rejects_changed_checks_or_foreign_repository(repo, tmp_path):
    artifact = run(repo, Writer())
    project = {**repo, 'checks': {'other': [sys.executable, '-c', 'pass']}}
    with pytest.raises(ExecutionError, match='checks changed'):
        run(project, Writer(), resume_artifacts=artifact)
    artifact['worktree'] = repo['workspace']
    with pytest.raises(ExecutionError, match='outside'):
        run(repo, Writer(), resume_artifacts=artifact)


def test_git_metadata_tampering_still_fails(repo):
    class Tamper(Writer):
        def run(self, request, emit, cancel=None):
            result = super().run(request, emit, cancel)
            subprocess.run(['git', 'config', 'diff.ignoreSubmodules', 'all'], cwd=request.workspace, check=True)
            return result
    with pytest.raises(ExecutionError, match='Git config'):
        run(repo, Tamper())


def test_non_transient_provider_error_does_not_repeat(repo):
    class Broken(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            raise ProviderError('invalid API credentials')
    runner = Broken()
    with pytest.raises(ExecutionError, match='invalid API credentials'):
        run(repo, runner)
    assert len(runner.requests) == 1


def test_terminal_command_evidence_survives_checkpoint_and_final_artifact(repo):
    class Evidence(Writer):
        def run(self, request, emit, cancel=None):
            result = super().run(request, emit, cancel)
            emit('command.completed', {'source': 'isolated_project_terminal', 'command': 'python main.py',
                                     'exit_code': 0, 'output': '14', 'duration_s': 0.1})
            return result
    events = []
    result = run(repo, Evidence(), emit=lambda *e: events.append(json.loads(json.dumps(e))))
    evidence = result['tasks'][0]['attempts'][0]['command_evidence']
    assert evidence[0]['output'] == '14'
    from factory.control.verification_evidence import render_evidence
    assert 'python main.py' in render_evidence(result)
    checkpoints = [e[1]['continuous_artifacts'] for e in events if e[0] == 'execution.checkpoint']
    assert checkpoints[-1]['tasks'][0]['attempts'][0]['command_evidence'] == evidence


def test_commit_failure_resumes_finalization_without_model_or_checks(repo, monkeypatch):
    import factory.control.continuous as module
    original=module._commit_tree
    monkeypatch.setattr(module,'_commit_tree',lambda *a,**k: (_ for _ in ()).throw(ExecutionError('git add failed')))
    runner=Writer()
    with pytest.raises(ExecutionError) as failure:run(repo,runner)
    saved=failure.value.artifacts
    assert saved['finalization_checkpoint']['checks'][0]['exit']==0
    monkeypatch.setattr(module,'_commit_tree',original)
    events=[]
    result=run(repo,runner,resume_artifacts=saved,task_fields={'resume_stage':'finalization'},emit=lambda *e:events.append(e))
    assert len(runner.requests)==1
    assert result['tasks'][0]['status']=='verified'
    assert not any(e[0]=='check.result' for e in events)


def test_recovery_rejects_source_changed_after_checks(repo, monkeypatch):
    import factory.control.continuous as module
    monkeypatch.setattr(module,'_commit_tree',lambda *a,**k: (_ for _ in ()).throw(ExecutionError('git add failed')))
    runner=Writer()
    with pytest.raises(ExecutionError) as failure:run(repo,runner)
    saved=failure.value.artifacts
    Path(saved['worktree'],'main.py').write_text('def double(n): return 0')
    with pytest.raises(ExecutionError,match='source changed after checks'):
        run(repo,runner,resume_artifacts=saved,task_fields={'resume_stage':'finalization'})
    assert len(runner.requests)==1


def test_verification_only_recovery_does_not_rerun_developer(repo):
    runner=Writer();saved=run(repo,runner)
    result=run(repo,runner,resume_artifacts=saved,task_fields={'resume_stage':'verification'})
    assert len(runner.requests)==1
    assert result['commit']==saved['commit']


def test_session_continuation_sends_feedback_not_full_context(repo):
    runner=Writer();saved=run(repo,runner)
    run(repo,runner,resume_artifacts=saved,task_fields={'prompt':'FULL_CONTEXT_MARKER'*1000,'resume_feedback':'Fix the requested rounding behavior'})
    assert runner.requests[-1].session_id=='session-1'
    assert 'Fix the requested rounding behavior' in runner.requests[-1].prompt
    assert 'FULL_CONTEXT_MARKER' not in runner.requests[-1].prompt
