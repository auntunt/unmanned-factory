"""Hosted execution bypasses model planning, preserves gates and resumes safely."""
import threading

import pytest

from factory.control.autonomy import DEFAULT_POLICY
from factory.control.execution import ExecutionError
from factory.control.planning import continuous_plan
from tests.test_control_app import login, project
from tests.test_workbench_app import app_env


def prepared(app_env, monkeypatch, request='Update greeting.txt'):
    client, store, svc, repo = app_env
    p = project(client, repo, login(client))
    current = svc.policies.get(p['id'])
    svc.policies.update(p['id'], dict(DEFAULT_POLICY), current['revision'], 'owner')
    submitted = []
    monkeypatch.setattr(svc, '_submit', lambda fn, rid: submitted.append((fn, rid)))
    run, _ = store.create_run(p['id'], request, source={'type': 'web', 'actor_id': 1})
    svc.start_plan(run['id'])
    svc.cancels[run['id']] = threading.Event()
    return store, svc, p, run['id'], submitted


def test_continuous_prepares_board_without_planner_call(app_env, monkeypatch):
    store, svc, p, rid, submitted = prepared(app_env, monkeypatch, 'x' * 20_000 + 'FINAL_REQUIREMENT')
    monkeypatch.setattr(svc.runner, 'run', lambda *args: pytest.fail('planner must not run'))
    svc._plan(rid)
    run = store.get(rid)
    assert run['execution_mode'] == 'continuous'
    assert run['status'] == 'queued', run
    assert [task['id'] for task in run['tasks']] == ['coding']
    assert 'x' * 20_000 + 'FINAL_REQUIREMENT' in run['plan']['tasks'][0]['prompt']
    assert run['plan']['tasks'][0]['checks'] == list(p['checks'])
    assert run['context']['commit_sha']
    assert svc._usage(rid, profile='planner')['calls'] == 0


def test_continuous_still_requires_approval_for_original_high_risk(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch, 'Change authentication')
    store.update(rid, {'status': 'needs_clarification'})
    svc.clarify(rid, 'yes', 'owner')
    monkeypatch.setattr(svc.runner, 'run', lambda *args: pytest.fail('planner must not run'))
    svc._plan(rid)
    run = store.get(rid)
    assert run['status'] == 'awaiting_approval'
    assert run['triage']['risk'] == 'high'


def test_existing_run_without_mode_keeps_model_planner(app_env, monkeypatch):
    client, store, svc, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Update greeting.txt')
    monkeypatch.setattr(svc, '_submit', lambda *args: None)
    svc.cancels[run['id']] = threading.Event()
    svc._plan(run['id'])
    assert 'execution_mode' not in store.get(run['id'])
    assert svc._usage(run['id'], profile='planner')['calls'] == 1


@pytest.mark.parametrize('failure', ['verdict', 'network', 'invalid'])
def test_review_repair_is_bounded_and_reuses_executor_session(app_env, monkeypatch, failure):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    store.update(rid, {'agent_snapshot': {'acceptance': []}})
    calls, reviews = [], []
    def executor(**kwargs):
        calls.append(kwargs)
        return {'worktree': p['workspace'], 'tasks': [{'id': 'coding', 'status': 'completed'}],
                'session_id': 'same-session', 'checks': [], 'known_cost_usd': 0}
    def verify(rid, run, project, config, artifacts):
        reviews.append(artifacts)
        if len(reviews) == 1:
            if failure != 'network':
                artifacts['verification'] = {'verdict': 'fail', 'reason': 'missing CSV',
                    **({'error_type': 'invalid_response'} if failure == 'invalid' else {})}
            raise ExecutionError('verification failed', artifacts=artifacts)
        artifacts['verification'] = {'verdict': 'pass', 'reason': 'CSV works'}
    svc.continuous_execute = executor
    monkeypatch.setattr(svc, '_independent_verify', verify)
    svc._run(rid)
    if failure == 'verdict':
        assert store.get(rid)['status'] == 'ready_for_review'
        assert len(calls) == len(reviews) == 2
        assert calls[1]['run_id'] == calls[0]['run_id']
        assert calls[1]['resume_artifacts']['session_id'] == 'same-session'
        assert 'missing CSV' in calls[1]['plan']['tasks'][0]['prompt']
    else:
        assert store.get(rid)['status'] == 'needs_human'
        assert len(calls) == 1


def test_repeated_failed_review_stops_after_one_repair(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    store.update(rid, {'agent_snapshot': {'acceptance': []}})
    calls = []
    def executor(**kwargs):
        calls.append(kwargs)
        return {'tasks': [], 'worktree': p['workspace'], 'known_cost_usd': 0}
    def verify(rid, run, project, config, artifacts):
        artifacts['verification'] = {'verdict': 'fail', 'reason': 'still broken'}
        raise ExecutionError('failed', artifacts=artifacts)
    svc.continuous_execute = executor
    monkeypatch.setattr(svc, '_independent_verify', verify)
    svc._run(rid)
    assert len(calls) == 2
    assert store.get(rid)['status'] == 'needs_human'
    assert store.get(rid)['artifacts']['verification_repair_count'] == 1


def test_continuous_restart_queues_same_session_but_never_replays_publish(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    run = store.get(rid)
    checkpoint = {'base_sha': run['context']['commit_sha'], 'worktree': p['workspace'],
        'session_id': 'durable-session', 'tasks': [{'id': 'coding', 'status': 'running'}]}
    store.update(rid, {'status': 'running'})
    store.append(rid, 'execution.checkpoint', {'execution_mode': 'continuous', 'continuous_artifacts': checkpoint})
    publishing, _ = store.create_run(p['id'], 'Publish')
    store.update(publishing['id'], {'status': 'publishing', 'execution_mode': 'continuous'})
    monkeypatch.setattr(svc, '_ensure_scheduler', lambda: None)
    with svc.lock:
        svc.recover()
        resumed = store.get(rid)
        assert resumed['status'] == 'queued'
        assert resumed['execution_resume']['artifacts']['session_id'] == 'durable-session'
        assert resumed['revision'] == run['revision']
        assert resumed['resume_count'] == 1
        assert store.get(publishing['id'])['status'] == 'needs_human'
        svc.queue.reset(rid)


def test_real_continuous_executor_gets_one_coding_turn_and_one_review(app_env, monkeypatch):
    import json
    from pathlib import Path
    from factory.control.providers import ProviderResult
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    calls = []
    def runner(request, emit, cancel=None):
        calls.append(request)
        if request.read_only:
            assert 'verdict' in request.prompt
            return ProviderResult(json.dumps({'verdict': 'pass', 'reason': 'greeting check passed'}), cost_usd=.01)
        (Path(request.workspace) / 'greeting.txt').write_text('hello world')
        return ProviderResult('done', cost_usd=.01, session_id='persistent-session')
    monkeypatch.setattr(svc.runner, 'run', runner)
    svc._plan(rid)
    assert calls == []
    svc._run(rid)
    run = store.get(rid)
    assert run['status'] == 'ready_for_review', run
    assert len(calls) == 2
    assert [call.read_only for call in calls] == [False, True]
    assert run['artifacts']['session_id'] == 'persistent-session'
    assert Path(p['workspace'], 'greeting.txt').read_text() == 'hello'


def test_continuous_rechecks_actor_before_execution(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    def denied(*args):
        raise ValueError('project access revoked')
    monkeypatch.setattr(svc.governance, 'require_project', denied)
    svc.continuous_execute = lambda **kwargs: pytest.fail('revoked actor must not execute')
    svc._run(rid)
    assert store.get(rid)['status'] == 'needs_human'


def test_execution_review_and_repair_share_one_deadline(app_env, monkeypatch):
    from types import SimpleNamespace
    from factory.control import service as service_module
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    clock = [100.0]
    monkeypatch.setattr(service_module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    timeouts = []
    def execute(**kwargs):
        timeouts.append(('execute', kwargs['timeout_s']))
        clock[0] += 10
        return {'tasks': [], 'worktree': p['workspace'], 'known_cost_usd': 0}
    reviews = []
    def verify(rid, run, project, config, artifacts):
        timeouts.append(('review', config['limits']['timeout_s']))
        clock[0] += 20
        reviews.append(artifacts)
        if len(reviews) == 1:
            artifacts['verification'] = {'verdict': 'fail', 'reason': 'fix output'}
            raise ExecutionError('failed', artifacts=artifacts)
        artifacts['verification'] = {'verdict': 'pass', 'reason': 'works'}
    svc.continuous_execute = execute
    monkeypatch.setattr(svc, '_independent_verify', verify)
    svc._run(rid)
    budget = svc.runtime_settings.get()['limits']['timeout_s']
    assert timeouts == [('execute', budget), ('review', budget - 10),
                        ('execute', budget - 30), ('review', budget - 40)]
    assert store.get(rid)['status'] == 'ready_for_review'
    from factory.control.autonomy import all_events
    checkpoints = [event['payload'] for event in all_events(store, rid) if event['type'] == 'execution.checkpoint']
    assert checkpoints[-1]['continuous_artifacts']['verification_repair_count'] == 1


@pytest.mark.parametrize('session_source', ['event', 'error'])
def test_review_transient_reconnect_keeps_session_and_records_each_call(app_env, monkeypatch, session_source):
    import json
    from types import SimpleNamespace
    from factory.control import service as service_module
    from factory.control.autonomy import all_events
    from factory.control.providers import ProviderError, ProviderResult
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    clock = [100.0]
    monkeypatch.setattr(service_module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    monkeypatch.setattr(svc.cancels[rid], 'wait', lambda seconds: False)
    executions, reviews = [], []
    def execute(**kwargs):
        executions.append(kwargs)
        return {'worktree': p['workspace'], 'tasks': [], 'session_id': 'coding-session', 'known_cost_usd': 0}
    def review(request, emit, cancel=None):
        reviews.append(request)
        assert request.read_only
        if len(reviews) == 1:
            clock[0] += 7
            if session_source == 'event':
                emit('provider.session', {'session_id': 'review-session'})
            raise ProviderError('504 Gateway Timeout', status_code=504,
                session_id='review-session' if session_source == 'error' else None)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': 'checked'}),
            cost_usd=.02, tokens_in=11, tokens_out=5)
    svc.continuous_execute = execute
    monkeypatch.setattr(svc.runner, 'run', review)
    svc._run(rid)
    run = store.get(rid)
    assert run['status'] == 'ready_for_review', run
    assert len(executions) == 1  # Transport recovery must not dispatch code repair.
    assert len(reviews) == 2
    assert reviews[0].session_id is None
    assert reviews[1].session_id == 'review-session'
    assert reviews[0].workspace == reviews[1].workspace == p['workspace']
    assert reviews[0].model == reviews[1].model
    assert reviews[0].provider == reviews[1].provider
    assert reviews[1].timeout_s <= reviews[0].timeout_s - 7
    usage = [event['payload'] for event in all_events(store, rid)
        if event['type'] == 'usage.recorded' and event['payload'].get('profile') == 'verification']
    assert len(usage) == 2
    assert usage[0]['call_id'] != usage[1]['call_id']
    assert usage[0]['cost_usd'] is None
    assert usage[1]['cost_usd'] == .02
    assert usage[1]['input_tokens'] == 11 and usage[1]['output_tokens'] == 5
    assert run['artifacts']['session_id'] == 'coding-session'
    assert run['artifacts']['verification_repair_count'] == 0


@pytest.mark.parametrize('cancel_first', [False, True])
def test_review_reconnect_failure_is_bounded_and_cancel_does_not_retry(app_env, monkeypatch, cancel_first):
    from factory.control.autonomy import all_events
    from factory.control.providers import ProviderError
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    monkeypatch.setattr(svc.cancels[rid], 'wait', lambda seconds: False)
    executions, reviews = [], []
    def execute(**kwargs):
        executions.append(kwargs)
        return {'worktree': p['workspace'], 'tasks': [], 'known_cost_usd': 0}
    def review(request, emit, cancel=None):
        reviews.append(request)
        if cancel_first:
            svc.cancel(rid, 'owner')
        raise ProviderError('504 Gateway Timeout', status_code=504, session_id='review-session')
    svc.continuous_execute = execute
    monkeypatch.setattr(svc.runner, 'run', review)
    svc._run(rid)
    assert len(executions) == 1
    assert len(reviews) == (1 if cancel_first else 2)
    assert store.get(rid)['status'] == ('cancelled' if cancel_first else 'needs_human')
    usage = [event for event in all_events(store, rid)
        if event['type'] == 'usage.recorded' and event['payload'].get('profile') == 'verification']
    assert len(usage) == len(reviews)


def test_review_precancel_does_not_report_an_unmade_provider_call(app_env, monkeypatch):
    from factory.control.autonomy import all_events
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    svc.cancels[rid].set()
    monkeypatch.setattr(svc.runner, 'run', lambda *args: pytest.fail('cancelled review must not dispatch'))
    with pytest.raises(ExecutionError):
        svc._independent_verify(rid, store.get(rid), p, svc.runtime_settings.get(), {'worktree': p['workspace']})
    assert not [event for event in all_events(store, rid)
        if event['type'] in ('provider.started', 'usage.recorded') and event['payload'].get('profile') == 'verification']
