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


def test_continuous_routes_only_on_current_request_not_prior_conversation(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch, '只修复数值输出边界')
    store.update(rid, {'history': ['上一轮已完成登录、密钥和部署配置']})
    svc._plan(rid)
    planned = store.get(rid)
    if planned['status'] == 'awaiting_approval':
        store.update(rid, {'status': 'queued'}, expected=('awaiting_approval',))
    captured = []

    def execute(**kwargs):
        captured.append(kwargs['plan']['tasks'][0])
        return {'worktree': p['workspace'], 'tasks': [{'id': 'coding', 'status': 'verified'}],
                'checks': [], 'known_cost_usd': 0}

    svc.continuous_execute = execute
    monkeypatch.setattr(svc, '_independent_verify', lambda *args: None)
    svc._run(rid)
    assert captured[0]['_routing_prompt'] == '只修复数值输出边界'
    assert '登录、密钥和部署' in captured[0]['prompt']


def test_existing_run_without_mode_keeps_model_planner(app_env, monkeypatch):
    client, store, svc, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Update greeting.txt')
    monkeypatch.setattr(svc, '_submit', lambda *args: None)
    svc.cancels[run['id']] = threading.Event()
    calls = []
    original = svc.runner.run
    def capture(request, emit, cancel=None):
        calls.append(request)
        return original(request, emit, cancel)
    monkeypatch.setattr(svc.runner, 'run', capture)
    svc._plan(run['id'])
    assert 'execution_mode' not in store.get(run['id'])
    assert svc._usage(run['id'], profile='planner')['calls'] == 1
    assert calls[0].max_budget_usd == p['budget_usd']


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


def test_budget_stop_before_review_repair_keeps_reason_for_blank_continuation(app_env, monkeypatch):
    from factory.control.autonomy import all_events
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    base_sha = store.get(rid)['context']['commit_sha']
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return {'execution_mode': 'continuous', 'base_sha': base_sha,
                'execution_checks': p['checks'], 'worktree': p['workspace'],
                'commit': base_sha, 'tasks': [{'id': 'coding', 'status': 'verified'}],
                'checks': [], 'known_cost_usd': 0}

    def verify(run_id, run, project, config, artifacts):
        store.append(run_id, 'usage.recorded', {
            'profile': 'verification', 'cost_usd': p['budget_usd']})
        artifacts['verification'] = {'verdict': 'fail', 'reason': '巨大指数被静默改成 0'}
        raise ExecutionError('独立验收失败', artifacts=artifacts)

    svc.continuous_execute = executor
    monkeypatch.setattr(svc, '_independent_verify', verify)
    svc._run(rid)
    stopped = store.get(rid)
    assert len(calls) == 1
    assert stopped['artifacts']['verification_repair_count'] == 0
    assert stopped['artifacts']['budget_exhausted'] is True
    assert '预算已用尽' in stopped['artifacts']['needs_human']
    assert not [event for event in all_events(store, rid)
                if event['type'] == 'verification.repair_started']

    current_project = store.project(p['id'])
    store.update_project(p['id'], {'budget_usd': p['budget_usd'] * 2},
                         current_project['revision'], 'owner')
    svc.continue_run(rid, '', stopped['revision'], stopped.get('resume_count', 0), 'owner')
    resumed = store.get(rid)
    assert '巨大指数被静默改成 0' in resumed['execution_resume']['answer']
    assert resumed['execution_resume']['resume_stage'] is None


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
            assert 'A README documenting a restriction does not authorize narrowing the contract' in request.prompt
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
    assert [call.max_budget_usd for call in calls] == pytest.approx(
        [p['budget_usd'], p['budget_usd'] - .01])
    assert run['artifacts']['session_id'] == 'persistent-session'
    assert Path(p['workspace'], 'greeting.txt').read_text() == 'hello'


def test_coding_call_over_budget_preserves_delivery_but_does_not_start_review(app_env, monkeypatch):
    from pathlib import Path
    from factory.control.autonomy import all_events
    from factory.control.providers import ProviderResult
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    calls = []

    def runner(request, emit, cancel=None):
        calls.append(request)
        if request.read_only:
            pytest.fail('review must not start after recorded coding cost exhausts the budget')
        Path(request.workspace, 'greeting.txt').write_text('hello world')
        return ProviderResult('done', cost_usd=p['budget_usd'] + 8.73,
                              session_id='expensive-session')

    monkeypatch.setattr(svc.runner, 'run', runner)
    svc._plan(rid)
    svc._run(rid)
    stopped = store.get(rid)
    assert stopped['status'] == 'needs_human'
    assert len(calls) == 1 and calls[0].read_only is False
    provider_events = [event for event in all_events(store, rid)
                       if event['type'] in ('provider.started', 'usage.recorded')]
    assert [event['type'] for event in provider_events] == ['provider.started', 'usage.recorded']
    assert stopped['artifacts']['commit']
    assert stopped['artifacts']['budget_exhausted'] is True
    assert stopped['artifacts']['total_known_cost_usd'] == pytest.approx(18.73)


def test_recorded_budget_exhaustion_blocks_a_new_planning_call(app_env, monkeypatch):
    client, store, svc, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Re-plan this task')
    svc.cancels[run['id']] = threading.Event()
    store.append(run['id'], 'usage.recorded', {
        'profile': 'standard', 'cost_usd': p['budget_usd']})
    monkeypatch.setattr(svc.runner, 'run',
                        lambda *args, **kwargs: pytest.fail('planner must not be dispatched'))
    svc._plan(run['id'])
    stopped = store.get(run['id'])
    assert stopped['status'] == 'needs_human'
    assert stopped['artifacts']['budget_exhausted'] is True
    assert '预算已用尽' in stopped['artifacts']['needs_human']


def test_planner_sdk_budget_stop_records_usage_and_visible_budget_state(app_env, monkeypatch):
    from factory.control.providers import ProviderError

    client, store, svc, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Plan a bounded change')
    svc.cancels[run['id']] = threading.Event()

    def stopped(request, emit, cancel=None):
        assert request.read_only is True
        assert request.max_budget_usd == p['budget_usd']
        emit('provider.usage', {'cost_usd': request.max_budget_usd,
                                'input_tokens': 50, 'output_tokens': 5})
        raise ProviderError('planner budget reached', transient=False,
                            error_kind='budget_exhausted')

    monkeypatch.setattr(svc.runner, 'run', stopped)
    svc._plan(run['id'])

    paused = store.get(run['id'])
    assert paused['status'] == 'needs_human'
    assert paused['artifacts']['budget_exhausted'] is True
    assert '规划达到' in paused['artifacts']['needs_human']
    assert svc._usage(run['id'], profile='planner') == {
        'known_cost_usd': p['budget_usd'], 'unknown_cost_calls': 0, 'calls': 1}


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
            emit('provider.usage', {'cost_usd': .01, 'input_tokens': 4,
                                    'output_tokens': 1})
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
    assert [request.max_budget_usd for request in reviews] == pytest.approx(
        [p['budget_usd'], p['budget_usd'] - .01])
    assert reviews[1].timeout_s <= reviews[0].timeout_s - 7
    usage = [event['payload'] for event in all_events(store, rid)
        if event['type'] == 'usage.recorded' and event['payload'].get('profile') == 'verification']
    assert len(usage) == 2
    assert usage[0]['call_id'] != usage[1]['call_id']
    assert usage[0]['cost_usd'] == .01
    assert usage[0]['input_tokens'] == 4 and usage[0]['output_tokens'] == 1
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


@pytest.mark.parametrize('stage', ['finalization', 'verification'])
def test_plain_continue_resumes_failed_platform_stage_but_new_feedback_runs_model(app_env, monkeypatch, stage):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    run = store.get(rid)
    artifacts = {'base_sha':run['context']['commit_sha'], 'commit':run['context']['commit_sha'],
                 'tasks':[{'id':'coding','status':'verified'}]}
    if stage=='finalization':artifacts['finalization_checkpoint']={'paths':[]}
    store.update(rid, {'status':'needs_human','artifacts':artifacts})
    svc.continue_run(rid, '', run['revision'], 0, 'owner')
    assert store.get(rid)['execution_resume']['resume_stage']==stage
    store.update(rid, {'status':'needs_human'})
    svc.continue_run(rid, '增加一个导出功能', run['revision'], 1, 'owner')
    assert store.get(rid)['execution_resume']['resume_stage'] is None
