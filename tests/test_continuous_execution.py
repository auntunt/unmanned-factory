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
    result = run({**repo, 'budget_usd': 2.0}, runner,
                 emit=lambda *event: events.append(json.loads(json.dumps(event))))
    assert len(runner.requests) == 1
    assert runner.requests[0].max_budget_usd == 2.0
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


def test_provider_call_keeps_one_stable_logical_id_across_quota_event(repo):
    class QuotaWriter(Writer):
        def run(self, request, emit, cancel=None):
            emit('quota.reserved', {'id': 'gateway-reservation'})
            return super().run(request, emit, cancel)

    events = []
    run({**repo, 'budget_usd': 2.0}, QuotaWriter(),
        emit=lambda *event: events.append(event))
    started = [payload for kind, payload, *_ in events if kind == 'provider.started']
    usage = [payload for kind, payload, *_ in events if kind == 'usage.recorded']
    assert len(started) == len(usage) == 1
    assert started[0]['call_id'] == usage[0]['call_id']
    assert started[0]['call_id'] != 'gateway-reservation'
    assert len(started[0]['call_id']) == 32


def test_unknown_cost_transient_failure_preserves_session_but_does_not_reuse_budget(repo, monkeypatch):
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
    with pytest.raises(ExecutionError, match='provider call not dispatched.*exhausted budget') as caught:
        run({**repo, 'budget_usd': 1.0}, runner, emit=lambda *e: events.append(e))
    assert len(runner.requests) == 1
    assert runner.requests[0].max_budget_usd == 1.0
    assert any(e[0] == 'execution.reconnecting' for e in events)
    assert caught.value.artifacts['session_id'] == 'session-1'
    assert caught.value.artifacts['unknown_cost_reserved_usd'] == pytest.approx(1.0)
    assert caught.value.artifacts['budget_exhausted'] is True


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
    result = run({**repo, 'budget_usd': 1.0}, runner)
    assert len(runner.requests) == 2
    assert [request.max_budget_usd for request in runner.requests] == pytest.approx([1.0, 0.9])
    assert result['tasks'][0]['attempts'][0]['checks'][0]['exit'] != 0
    assert result['checks'][0]['exit'] == 0


def test_final_check_repair_is_not_dispatched_after_known_budget_is_exhausted(repo):
    class ExpensiveDraft(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text(
                'def double(n): return n + 2  # incorrect first draft\n')
            return ProviderResult(text='first draft', session_id='session-1', cost_usd=0.1)

    runner = ExpensiveDraft()
    events = []
    with pytest.raises(ExecutionError, match='provider call not dispatched.*exhausted budget') as caught:
        run({**repo, 'budget_usd': 0.1}, runner, emit=lambda *event: events.append(event))
    assert len(runner.requests) == 1
    assert len([event for event in events if event[0] == 'provider.started']) == 1
    assert len([event for event in events if event[0] == 'usage.recorded']) == 1
    assert caught.value.artifacts['known_cost_usd'] == pytest.approx(0.1)
    assert caught.value.artifacts['autopublish_blocked'] is True


def test_unknown_cost_reserves_ceiling_and_blocks_a_paid_check_repair(repo):
    class UnknownFirstCost(Writer):
        def run(self, request, emit, cancel=None):
            if not self.requests:
                self.requests.append(request)
                Path(request.workspace, 'main.py').write_text(
                    'def double(n): return n + 2  # incorrect first draft\n')
                return ProviderResult(text='first draft', session_id='session-1', cost_usd=None)
            return super().run(request, emit, cancel)

    runner = UnknownFirstCost()
    with pytest.raises(ExecutionError, match='provider call not dispatched.*exhausted budget') as caught:
        run({**repo, 'budget_usd': 1.0, 'unknown_cost_policy': 'allow_bounded'}, runner)
    assert len(runner.requests) == 1
    assert caught.value.artifacts['observed_cost_usd'] is None
    assert caught.value.artifacts['known_cost_usd'] == 0
    assert caught.value.artifacts['unknown_cost_reserved_usd'] == pytest.approx(1.0)


def test_unknown_cost_stop_still_blocks_a_second_paid_call(repo):
    class UnknownFirstCost(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text(
                'def double(n): return n + 2  # incorrect first draft\n')
            return ProviderResult(text='first draft', session_id='session-1', cost_usd=None)

    runner = UnknownFirstCost()
    with pytest.raises(ExecutionError, match='cost is unknown'):
        run({**repo, 'budget_usd': 1.0, 'unknown_cost_policy': 'stop'}, runner)
    assert len(runner.requests) == 1


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


def test_provider_budget_stop_preserves_continuation_checkpoint(repo):
    class BudgetStopped(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('draft preserved\n')
            emit('provider.session', {'session_id': 'budget-session'})
            emit('provider.usage', {'cost_usd': request.max_budget_usd,
                                    'input_tokens': 100, 'output_tokens': 10})
            raise ProviderError('call budget reached', session_id='budget-session',
                                transient=False, error_kind='budget_exhausted')

    runner = BudgetStopped()
    with pytest.raises(ExecutionError, match='coding stopped at project budget') as stopped:
        run({**repo, 'budget_usd': 0.5}, runner)
    artifacts = stopped.value.artifacts
    assert len(runner.requests) == 1
    assert runner.requests[0].max_budget_usd == 0.5
    assert artifacts['session_id'] == 'budget-session'
    assert artifacts['budget_exhausted'] is True
    assert artifacts['autopublish_blocked'] is True
    assert artifacts['known_cost_usd'] == pytest.approx(.5)
    assert Path(artifacts['worktree'], 'main.py').read_text() == 'draft preserved\n'
    assert artifacts['tasks'][0]['attempts'][0]['session_id'] == 'budget-session'


def test_provider_budget_stop_after_successful_work_runs_trusted_checks_and_commits(repo):
    class FinishedBeforeBudgetStop(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n * 2\n')
            emit('provider.session', {'session_id': 'budget-session'})
            emit('command.completed', {'source': 'isolated_project_terminal',
                'command': 'python -m pytest', 'exit_code': 0, 'output': '50 passed'})
            emit('provider.usage', {'cost_usd': request.max_budget_usd,
                                    'input_tokens': 100, 'output_tokens': 10})
            raise ProviderError('call budget reached', session_id='budget-session',
                                transient=False, error_kind='budget_exhausted')

    runner = FinishedBeforeBudgetStop()
    events = []
    result = run({**repo, 'budget_usd': 0.5}, runner,
                 emit=lambda *event: events.append(event))
    assert len(runner.requests) == 1
    assert result['commit']
    assert result['coding_budget_reached'] is True
    assert result['checks'][0]['exit'] == 0
    assert result['tasks'][0]['attempts'][0]['provider_stop'] == 'budget_exhausted'
    assert result['tasks'][0]['attempts'][0]['status'] == 'verified'
    assert any(event[0] == 'execution.completed_after_budget_stop' for event in events)


def test_provider_budget_stop_never_uses_worker_evidence_instead_of_trusted_checks(repo):
    class FalsePositiveEvidence(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n + 2\n')
            emit('command.completed', {'source': 'isolated_project_terminal',
                'command': 'echo tests passed', 'exit_code': 0, 'output': '50 passed'})
            emit('provider.usage', {'cost_usd': request.max_budget_usd})
            raise ProviderError('call budget reached', transient=False,
                                error_kind='budget_exhausted')

    with pytest.raises(ExecutionError, match='coding stopped at project budget') as stopped:
        run({**repo, 'budget_usd': 0.5}, FalsePositiveEvidence())
    artifacts = stopped.value.artifacts
    assert artifacts['commit'] is None
    assert artifacts['checks'][0]['exit'] != 0
    assert artifacts['budget_exhausted'] is True


def test_legacy_budget_stop_can_rerun_checks_and_commit_without_model(repo):
    class OldBudgetStop(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n * 2\n')
            emit('provider.session', {'session_id': 'old-budget-session'})
            emit('provider.usage', {'cost_usd': request.max_budget_usd})
            raise ProviderError('call budget reached', transient=False,
                                error_kind='budget_exhausted')

    old_runner = OldBudgetStop()
    with pytest.raises(ExecutionError) as stopped:
        run({**repo, 'budget_usd': 0.5}, old_runner)
    saved = json.loads(json.dumps(stopped.value.artifacts))
    saved['tasks'][0]['attempts'][0]['command_evidence'] = [{
        'command': 'python -m pytest', 'exit_code': 0, 'output': '50 passed'}]

    class NoModel:
        def __init__(self):
            self.requests = []
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            raise AssertionError('budget finalization must not call a model')

    runner = NoModel()
    events = []
    result = run({**repo, 'budget_usd': 0.0}, runner, resume_artifacts=saved,
        task_fields={'resume_stage': 'budget_finalization'},
        emit=lambda *event: events.append(event))
    assert runner.requests == []
    assert result['commit']
    assert result['checks'][0]['exit'] == 0
    assert result['tasks'][0]['attempts'][-1]['recovery_stage'] == 'budget_finalization'
    assert any(event[0] == 'execution.reused'
               and event[1].get('stage') == 'budget_finalization' for event in events)


def test_budget_finalization_rejects_known_failed_platform_checks(repo):
    class OldBudgetStop(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n + 2\n')
            emit('provider.session', {'session_id': 'old-budget-session'})
            raise ProviderError('call budget reached', transient=False,
                                error_kind='budget_exhausted')

    with pytest.raises(ExecutionError) as stopped:
        run({**repo, 'budget_usd': 0.5}, OldBudgetStop())
    saved = json.loads(json.dumps(stopped.value.artifacts))
    saved['tasks'][0]['attempts'][0]['command_evidence'] = [{
        'command': 'python -m pytest', 'exit_code': 0, 'output': 'tests ran'}]
    saved['checks'] = [{'name': 'behavior', 'argv': repo['checks']['behavior'],
                        'exit': 1, 'stdout': '', 'stderr': 'assertion failed'}]
    assert saved['checks'][0]['exit'] != 0

    with pytest.raises(ExecutionError, match='known failing configured check'):
        run({**repo, 'budget_usd': 1.0}, Writer(), resume_artifacts=saved,
            task_fields={'resume_stage': 'budget_finalization'})


def test_cancel_after_recovery_check_never_commits(repo, monkeypatch):
    class OldBudgetStop(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n * 2\n')
            emit('provider.session', {'session_id': 'old-budget-session'})
            raise ProviderError('call budget reached', transient=False,
                                error_kind='budget_exhausted')

    with pytest.raises(ExecutionError) as stopped:
        run({**repo, 'budget_usd': 0.5}, OldBudgetStop())
    saved = json.loads(json.dumps(stopped.value.artifacts))
    saved['tasks'][0]['attempts'][0]['command_evidence'] = [{
        'command': 'python -m pytest', 'exit_code': 0, 'output': '50 passed'}]
    cancel = threading.Event()

    def cancel_after_success(root, name, argv, timeout_s, emit, task_id, event):
        event.set()
        return {'name': name, 'argv': argv, 'exit': 0, 'stdout': 'passed',
                'stderr': '', 'duration_s': 0.01}

    monkeypatch.setattr('factory.control.continuous._run_check', cancel_after_success)
    with pytest.raises(ExecutionError, match='execution cancelled') as cancelled:
        run({**repo, 'budget_usd': 0.0}, Writer(), resume_artifacts=saved,
            task_fields={'resume_stage': 'budget_finalization'}, cancel=cancel)
    assert cancelled.value.artifacts['commit'] is None
    assert cancelled.value.artifacts['tasks'][0]['status'] == 'cancelled'


def test_cancelled_saved_check_is_never_treated_as_finalization_checkpoint(repo):
    class Interrupted(Writer):
        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            Path(request.workspace, 'main.py').write_text('def double(n): return n * 2\n')
            raise ProviderError('interrupted before finalization')

    with pytest.raises(ExecutionError) as stopped:
        run(repo, Interrupted())
    saved = json.loads(json.dumps(stopped.value.artifacts))
    saved['finalization_checkpoint'] = {'paths': ['main.py'], 'signature': 'unused',
        'checks': [{'name': 'behavior', 'exit': None, 'cancelled': True}]}

    with pytest.raises(ExecutionError, match='failed or cancelled check') as rejected:
        run(repo, Writer(), resume_artifacts=saved,
            task_fields={'resume_stage': 'finalization'})
    assert rejected.value.artifacts['commit'] is None


def test_cancel_at_commit_entry_preserves_uncommitted_work(repo, monkeypatch):
    from factory.control import continuous as continuous_module
    cancel = threading.Event()
    original_commit = continuous_module._commit_tree

    def cancel_at_entry(*args, **kwargs):
        cancel.set()
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(continuous_module, '_commit_tree', cancel_at_entry)
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                   cwd=repo['workspace'], text=True).strip()
    with pytest.raises(ExecutionError, match='execution cancelled') as stopped:
        run(repo, Writer(), cancel=cancel)
    worktree = stopped.value.artifacts['worktree']
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                   cwd=worktree, text=True).strip() == base
    assert Path(worktree, 'main.py').read_text() == 'def double(n): return n * 2\n'
    assert stopped.value.artifacts['commit'] is None


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


def test_feedback_successor_reuses_session_in_fresh_worktree_with_delta_prompt(repo):
    first = run(repo, Writer(), run_id='first-run')
    predecessor_workspace = first['worktree']
    predecessor_branch = first['branch']
    events = []

    class FeedbackWriter:
        def __init__(self):
            self.requests = []

        def run(self, request, emit, cancel=None):
            self.requests.append(request)
            assert request.session_id == 'session-1'
            assert request.workspace != predecessor_workspace
            assert Path(request.workspace, 'main.py').read_text() == 'def double(n): return n * 2\n'
            Path(request.workspace, 'feedback.txt').write_text('decimal caps removed\n')
            emit('provider.session', {'session_id': 'session-1'})
            return ProviderResult('feedback applied', session_id='session-1', cost_usd=0.1)

    runner = FeedbackWriter()
    successor_project = {**repo,
        'workspace': predecessor_workspace,
        'base_branch': predecessor_branch}
    result = run(successor_project, runner, run_id='feedback-successor',
        task_fields={
            'prompt': 'FULL_SUCCESSOR_CONTEXT_MARKER' * 1000,
            'resume_feedback': 'Remove only the unrequested decimal caps.',
            '_feedback_session': {
                'session_id': 'session-1',
                'session_profile': {'provider': 'claude', 'model': 'model'},
                'previous_run_id': 'first-run',
            },
        }, emit=lambda *event: events.append(event))

    assert result['worktree'] != predecessor_workspace
    assert result['branch'] != predecessor_branch
    assert result['base_sha'] == first['commit']
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'],
        cwd=predecessor_workspace, text=True).strip() == first['commit']
    request = runner.requests[0]
    assert 'Remove only the unrequested decimal caps.' in request.prompt
    assert 'FULL_SUCCESSOR_CONTEXT_MARKER' not in request.prompt
    delivery = next(payload for kind, payload, *_ in events
                    if kind == 'execution.context_delivery')
    assert delivery['session_resumed'] is True
    assert delivery['session_source'] == 'feedback_predecessor'
    assert delivery['sent_prompt_chars'] < delivery['full_prompt_chars']
    continuation = next(payload for kind, payload, *_ in events
                        if kind == 'execution.session_continuation')
    assert continuation == {
        'source': 'feedback_predecessor',
        'previous_run_id': 'first-run',
        'reused': True,
        'previous_model': 'model',
        'current_model': 'model',
        'model_changed': False,
    }


def test_feedback_successor_reuses_session_after_same_provider_model_change(repo):
    first = run(repo, Writer(), run_id='first-run')
    events = []
    runner = Writer()
    successor_project = {**repo,
        'workspace': first['worktree'],
        'base_branch': first['branch']}

    result = run(successor_project, runner, run_id='feedback-new-model',
        task_fields={
            'prompt': 'FULL_CONTEXT_FOR_NEW_MODEL' * 1000,
            'resume_feedback': 'Apply the narrow feedback.',
            '_feedback_session': {
                'session_id': 'session-1',
                'session_profile': {'provider': 'claude', 'model': 'previous-model'},
                'previous_run_id': 'first-run',
            },
        }, emit=lambda *event: events.append(event))

    assert runner.requests[0].session_id == 'session-1'
    assert runner.requests[0].workspace != first['worktree']
    assert result['branch'] != first['branch']
    assert 'Apply the narrow feedback.' in runner.requests[0].prompt
    assert 'FULL_CONTEXT_FOR_NEW_MODEL' not in runner.requests[0].prompt
    delivery = next(payload for kind, payload, *_ in events
                    if kind == 'execution.context_delivery')
    assert delivery['session_resumed'] is True
    assert delivery['session_source'] == 'feedback_predecessor'
    assert delivery['sent_prompt_chars'] < delivery['full_prompt_chars']
    continuation = next(payload for kind, payload, *_ in events
                        if kind == 'execution.session_continuation')
    assert continuation['reused'] is True
    assert continuation['previous_model'] == 'previous-model'
    assert continuation['current_model'] == 'model'
    assert continuation['model_changed'] is True
    assert 'reason' not in continuation


def test_feedback_successor_does_not_reuse_session_after_provider_change(repo):
    first = run(repo, Writer(), run_id='first-run')
    events = []
    runner = Writer()
    successor_project = {**repo,
        'workspace': first['worktree'],
        'base_branch': first['branch']}

    result = run(successor_project, runner, run_id='feedback-new-provider',
        task_fields={
            'prompt': 'FULL_CONTEXT_FOR_NEW_PROVIDER',
            'resume_feedback': 'Apply the narrow feedback.',
            '_feedback_session': {
                'session_id': 'session-1',
                'session_profile': {'provider': 'codex', 'model': 'model'},
                'previous_run_id': 'first-run',
            },
        }, emit=lambda *event: events.append(event))

    assert runner.requests[0].session_id is None
    assert runner.requests[0].workspace != first['worktree']
    assert result['branch'] != first['branch']
    assert 'FULL_CONTEXT_FOR_NEW_PROVIDER' in runner.requests[0].prompt
    delivery = next(payload for kind, payload, *_ in events
                    if kind == 'execution.context_delivery')
    assert delivery['session_resumed'] is False
    assert delivery['session_source'] is None
    assert delivery['sent_prompt_chars'] == delivery['full_prompt_chars']
    continuation = next(payload for kind, payload, *_ in events
                        if kind == 'execution.session_continuation')
    assert continuation['reused'] is False
    assert continuation['reason'] == 'provider_changed'
    assert continuation['previous_model'] == 'model'
    assert continuation['current_model'] == 'model'
    assert continuation['model_changed'] is False
