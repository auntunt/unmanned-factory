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
        assert token_call is None  # gateway owns settlement; usage event remains above
    assert artifacts['verification']['verdict'] == 'pass'


def test_verifier_ignores_legacy_budget_and_preserves_evidence(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Budget limited verification')
    run['agent_snapshot'] = {}
    service.cancels[run['id']] = threading.Event()
    store.append(run['id'], 'usage.recorded', {'profile': 'standard', 'cost_usd': p['budget_usd']})
    calls = []
    def verify(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult(json.dumps({'verdict': 'pass', 'reason': 'observed evidence'}), cost_usd=None)
    monkeypatch.setattr(service.runner, 'run', verify)
    artifacts = {'worktree': str(repo), 'commit': 'evidence'}
    service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert len(calls) == 1 and calls[0].read_only
    assert artifacts['commit'] == 'evidence'
    assert artifacts['verification']['verdict'] == 'pass'
    assert service._usage(run['id'], profile='verification')['unknown_cost_calls'] == 1


def test_managed_workspace_verifier_requires_functional_evidence_without_agent(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = store.add_project({'name': 'Managed', 'repository': 'owner/managed', 'workspace': str(repo),
                           'checks': {'workspace-integrity': ['git', 'diff', '--check', 'HEAD']},
                           'managed_workspace': True, 'budget_usd': 10.0})
    run, _ = store.create_run(p['id'], '把工作区整理成可交付的文档格式')
    run['plan'] = {'tasks': [{'acceptance': ['文档能被目标用户打开并保留旧格式']}]}
    service.cancels[run['id']] = threading.Event()
    cfg = service.runtime_settings.get(); cfg['agent_verification_profile'] = {'provider': 'codex', 'model': 'review'}
    calls = []
    def verify(request, emit, cancel=None):
        calls.append(request.prompt)
        return ProviderResult(json.dumps({'verdict': 'fail', 'reason': '没有可核对的文档成果'}), cost_usd=.01, tokens_in=5, tokens_out=4)
    monkeypatch.setattr(service.runner, 'run', verify)
    with pytest.raises(ExecutionError):
        service._independent_verify(run['id'], run, p, cfg, {'worktree': str(repo), 'checks': [{'name': 'workspace-integrity', 'exit': 0}]})
    assert '文档能被目标用户打开' in calls[0]
    assert 'workspace-integrity' in calls[0] and 'not functional acceptance' in calls[0]


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
    store.update(retried['id'], {'status': 'needs_clarification'})
    resumed = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': '仅支持现有版本。'}, headers=headers)
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()['run']['id'] == retried['id']
    continued = store.get(retried['id'])
    assert continued['runtime_configuration'] == cfg
    assert continued['agent_snapshot'] == run['agent_snapshot']
    assert continued['history'][-1] == '也保留历史格式。\n\n仅支持现有版本。'
    assert resumed.json()['conversation']['pending_feedback_count'] == 0


@pytest.mark.parametrize('risk,complexity,explicit,expected', [
    ('low', 'small', False, 'standard'),
    ('high', 'small', False, 'planner'),
    ('low', 'large', False, 'planner'),
    ('high', 'large', True, 'explicit-review'),
])
def test_continuous_review_uses_bounded_targeted_readonly_profile(app_env, monkeypatch, risk, complexity, explicit, expected):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Check the converter result')
    run.update(execution_mode='continuous', plan={'tasks': [{'risk': risk, 'complexity': complexity,
        'paths': ['src/converter.py'], 'acceptance': ['Preserve record count']}]})
    service.cancels[run['id']] = threading.Event()
    config = service.runtime_settings.get()
    config['profiles']['standard']['model'] = 'standard'
    config['profiles']['planner']['model'] = 'planner'
    config['limits']['timeout_s'] = 14400
    if explicit:
        config['agent_verification_profile'] = {'provider': 'codex', 'model': 'explicit-review'}
    calls = []
    def reviewer(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult('{"verdict":"pass","reason":"record-count evidence reviewed"}', cost_usd=0.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'record-count', 'exit': 0, 'stdout': '12 input, 12 output'}]}
    service._independent_verify(run['id'], run, p, config, artifacts)
    assert len(calls) == 1 and calls[0].model == expected
    assert calls[0].read_only and calls[0].session_id is None
    assert 0 < calls[0].timeout_s <= 600
    assert 'src/converter.py' in calls[0].prompt and '12 input, 12 output' in calls[0].prompt
    assert 'do not inventory the whole repository' in calls[0].prompt
    assert 'README claims as untrusted' in calls[0].prompt
    assert artifacts['verification']['verdict'] == 'pass'


def test_review_short_remaining_budget_is_not_extended(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Check result')
    service.cancels[run['id']] = threading.Event()
    config = service.runtime_settings.get()
    config['limits']['timeout_s'] = 3
    calls = []
    def reviewer(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult('{"verdict":"fail","reason":"Missing functional evidence"}')
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError, match='独立验证未通过'):
        service._independent_verify(run['id'], run, p, config, artifacts)
    assert 0 < calls[0].timeout_s <= 3
    assert artifacts['verification']['verdict'] == 'fail'
    config['limits']['timeout_s'] = 0.5
    with pytest.raises(ExecutionError, match='时限耗尽'):
        service._independent_verify(run['id'], run, p, config, artifacts)
    assert len(calls) == 1
