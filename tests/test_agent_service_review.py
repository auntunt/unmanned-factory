"""Integration review probes for the shared runtime, beyond CRUD success."""
import json
import threading
import time

import pytest

from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Conflict
from tests.test_control_app import login, project
from tests.test_workbench_app import app_env


def wait_job(service, jid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = service.maintenance_status(jid)
        if job['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
            return job
        time.sleep(.01)
    raise AssertionError('job did not terminate')


def test_job_claim_cancel_and_durable_terminal(app_env):
    _, store, service, _ = app_env
    def work(cancel):
        cancel.wait(2)
        return {'status': 'cancelled'}
    job = service.start_maintenance(work, conversation_id='same', actor_id=1)
    with pytest.raises(Conflict):
        service.start_maintenance(work, conversation_id='same', actor_id=1)
    service.cancel_maintenance(job['id'], '1')
    assert wait_job(service, job['id'])['status'] == 'cancelled'
    service.maintenance_jobs.clear()
    assert service.maintenance_status(job['id'])['result'] == {'status': 'cancelled'}
    assert service.cancel_maintenance(job['id'], '1')['status'] == 'cancelled'


def test_verifier_uses_separate_profile_readonly_context_and_records_usage(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Verify specific acceptance')
    run['agent_snapshot'] = {'acceptance': ['Keep compatibility']}
    service.cancels[run['id']] = threading.Event()
    cfg = service.runtime_settings.get()
    cfg['agent_verification_profile'] = {'provider': 'codex', 'model': 'independent-review'}
    calls = []
    def verify(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': 'observed evidence'}), cost_usd=.03, tokens_in=10, tokens_out=5)
    monkeypatch.setattr(service.runner, 'run', verify)
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'check', 'exit': 0}]}
    service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert calls[0].model == 'independent-review'
    assert calls[0].read_only and calls[0].session_id is None
    assert 'Keep compatibility' in calls[0].prompt
    assert service._usage(run['id'], profile='verification') == {'known_cost_usd': .03, 'unknown_cost_calls': 0, 'calls': 1}
    with service.governance.connect() as db:
        token_call = db.execute('SELECT * FROM token_calls WHERE run_id=?', (run['id'],)).fetchone()
        assert token_call['status'] == 'settled' and token_call['actual_tokens'] == 15
    assert artifacts['verification']['verdict'] == 'pass'


def test_verifier_budget_stops_before_dispatch_and_preserves_evidence(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Budget limited verification')
    run['agent_snapshot'] = {}
    service.cancels[run['id']] = threading.Event()
    store.append(run['id'], 'usage.recorded', {'profile': 'standard', 'cost_usd': p['budget_usd']})
    def forbidden(*args, **kwargs):
        pytest.fail('budget exhausted, provider must not be called')
    monkeypatch.setattr(service.runner, 'run', forbidden)
    artifacts = {'worktree': str(repo), 'commit': 'evidence'}
    with pytest.raises(ExecutionError) as exc:
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert exc.value.artifacts == artifacts


def test_restart_marks_unacknowledged_job_interrupted(app_env):
    _, store, service, _ = app_env
    with store.connect() as db:
        db.execute("INSERT INTO maintenance_jobs VALUES('interrupted-job','conversation',1,'running',NULL,NULL,'before','before')")
    another = Service(store, runner=service.runner, profiles=service.profiles)
    try:
        assert another.maintenance_status('interrupted-job')['status'] == 'interrupted'
    finally:
        another.close()


def test_stage_profiles_are_independent_and_retry_keeps_snapshot(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    settings = {stage: {'provider': 'codex', 'model': name} for stage, name in {
        'default': 'default-model', 'planning': 'planning-model',
        'execution': 'execution-model', 'verification': 'verification-model',
    }.items()}
    agent = client.post('/api/v4/agents', json={'name': '模型分工', 'model_settings': settings}, headers=headers).json()
    c = client.post(f"/api/v4/agents/{agent['id']}/conversations", json={'mode': 'do', 'project_id': p['id']}, headers=headers).json()
    monkeypatch.setattr(service, 'start_plan', lambda rid: None)
    response = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': '更新文档，并独立验证。'}, headers=headers)
    assert response.status_code == 201, response.text
    run = response.json()['run']
    cfg = run['runtime_configuration']
    assert cfg['profiles']['planner']['model'] == 'planning-model'
    assert all(cfg['profiles'][k]['model'] == 'execution-model' for k in ('cheap', 'standard', 'strong'))
    assert cfg['agent_verification_profile']['model'] == 'verification-model'
    again = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': '也保留历史格式。'}, headers=headers).json()
    assert again['run']['id'] == run['id'] and again['waiting_for_active_run']
    store.update(run['id'], {'status': 'needs_human'})
    retried = service.retry(run['id'], 'owner', actor_id=1)
    assert retried['agent_snapshot'] == run['agent_snapshot']
    assert retried['runtime_configuration'] == cfg
    store.update(run['id'], {'status': 'needs_clarification'})
    resumed = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': '仅支持现有版本。'}, headers=headers)
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()['run']['id'] == run['id']
    continued = store.get(run['id'])
    assert continued['runtime_configuration'] == cfg
    assert continued['agent_snapshot'] == run['agent_snapshot']
    assert continued['history'][-1] == '仅支持现有版本。'
