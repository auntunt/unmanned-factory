# Legacy execution assertions explicitly use maintenance; default general confirmation is covered in test_requirement_analysis.py.
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from factory.control.operation_presets import OperationStore
from factory.control.store import Conflict
from factory.control.inspections import inspect_run
from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from tests.test_control_app import app_env, login, project
from tests.test_active_verification import setup_review
from tests.review_helpers import passing_review


def test_catalog_database_version_and_legacy_fingerprint(app_env):
    client, store, service, repo = app_env
    catalog = OperationStore(store)
    _, digest, preset = catalog.compile('bugfix', '问题')
    assert digest == hashlib.sha256('bugfix\0问题'.encode()).hexdigest()
    updated = {**preset, 'version': 2, 'brief': '新的诊断步骤', 'label': '修复问题'}
    with store.connect() as db:
        db.execute('INSERT INTO operation_presets VALUES(?,?,?)', ('bugfix', 2, json.dumps(updated)))
    headers = login(client)
    visible = client.get('/api/v2/operation-presets').json()['presets']
    assert next(x for x in visible if x['id'] == 'bugfix')['version'] == 2
    text, new_digest, actual = OperationStore(store).compile('bugfix', '问题')
    assert '新的诊断步骤' in text and actual['version'] == 2
    assert digest == new_digest


def test_fields_fingerprint_conflict_and_reordered_retry(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client); pid = project(client, repo, headers)['id']
    calls = []; monkeypatch.setattr(service, 'start_plan', calls.append)
    body = dict(project_id=pid, request='启动报错', operation='startup', idempotency_key='field-key-123', operation_fields={'port': '8080', 'command': 'npm start'})
    first = client.post('/api/v2/runs', json=body, headers=headers)
    assert first.status_code == 201
    assert '启动命令：npm start' in first.json()['request']
    again = client.post('/api/v2/runs', json={'operation': 'bugfix', **body, 'operation_fields': {'command': ' npm start ', 'port': '8080'}}, headers=headers)
    assert again.json()['id'] == first.json()['id'] and len(calls) == 1
    changed = client.post('/api/v2/runs', json={'operation': 'bugfix', **body, 'operation_fields': {'port': '9090'}}, headers=headers)
    assert changed.status_code == 409 and len(calls) == 1
    invalid = client.post('/api/v2/runs', json={'operation': 'bugfix', **body, 'operation_fields': {'remote_password': 'abc'}}, headers=headers)
    assert invalid.status_code == 422


def schedule(app_env):
    client, store, service, repo = app_env
    headers = login(client); p = project(client, repo, headers)
    config = service.inspections.configure(p['id'], enabled=True, interval_s=300, revision=0, actor_id=1)
    return service, store, p, config


def test_inspection_disabled_atomic_queue_and_no_backlog(app_env):
    service, store, p, config = schedule(app_env)
    assert service.inspections.tick(config['next_at'] - 1) == []
    ids = service.inspections.tick(config['next_at'])
    assert len(ids) == 1
    assert service.queue.pending()[0]['run_id'] == ids[0]
    assert service.inspections.tick(config['next_at'] + 9999) == []  # active same project
    store.update(ids[0], {'status': 'inspection_completed'})
    next_ids = service.inspections.tick(config['next_at'] + 9999)
    assert len(next_ids) == 1  # one catch-up, never every missed interval
    service.inspections.configure(p['id'], enabled=False, interval_s=300, revision=1, actor_id=1)
    store.update(next_ids[0], {'status': 'inspection_completed'})
    assert service.inspections.tick(config['next_at'] + 999999) == []
    with pytest.raises(Conflict):
        service.inspections.configure(p['id'], enabled=True, interval_s=300, revision=1, actor_id=1)


def test_inspection_budget_blocks_provider_and_no_coding(app_env, monkeypatch):
    service, store, p, config = schedule(app_env)
    rid = service.inspections.tick(config['next_at'])[0]
    def exhausted(*args): raise Conflict('budget exhausted')
    monkeypatch.setattr(service, '_remaining_dollar_budget', exhausted)
    monkeypatch.setattr(service, '_independent_verify', lambda *args: pytest.fail('must not call provider'))
    inspect_run(service, rid)
    assert store.get(rid)['status'] == 'inspection_failed'
    assert 'budget exhausted' in store.get(rid)['error']
    for fn, args in [(service.start_plan, (rid,)), (service.publish, (rid,)), (service.publish_github, (rid,))]:
        with pytest.raises(Conflict): fn(*args)


def test_inspection_uses_isolated_verifier_preserves_original(app_env, monkeypatch):
    service, store, p, config = schedule(app_env)
    rid = service.inspections.tick(config['next_at'])[0]
    service.cancels[rid] = threading.Event()
    original = (Path(p['workspace']) / 'greeting.txt').read_bytes()
    def review(request, emit, cancel=None):
        assert request.read_only and request.verification
        assert Path(request.workspace) != Path(p['workspace'])
        return ProviderResult(passing_review(request, '启动检查通过'), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', review)
    inspect_run(service, rid)
    assert store.get(rid)['status'] == 'inspection_completed'
    assert (Path(p['workspace']) / 'greeting.txt').read_bytes() == original
    assert not store.get(rid).get('capability_candidate_id')


def test_unavailable_browser_preserves_other_criteria_without_repair(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    calls = []
    def reviewer(request, emit, cancel=None):
        calls.append(request)
        emit('browser.observed', {'action': 'open', 'ok': False, 'error_type': 'browser_unavailable', 'error': 'Chrome startup timeout'})
        return ProviderResult(passing_review(request, '其他检查已通过'), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError):
        service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert len(calls) == 1
    assert artifacts['verification']['verdict'] == 'unverified'
    assert artifacts['verification']['error_type'] == 'unverified'
    assert artifacts['acceptance_ledger']['counts'] == {'pass': 1, 'fail': 0, 'unverified': 1}


def test_real_failure_not_hidden_by_browser_unavailable(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    def reviewer(request, emit, cancel=None):
        emit('browser.observed', {'ok': False, 'error_type': 'browser_unavailable', 'error': 'missing'})
        verdict = json.loads(passing_review(request, 'calculation wrong'))
        verdict['verdict'] = 'fail'; verdict['criteria'][0]['status'] = 'fail'
        return ProviderResult(json.dumps(verdict), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError): service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert artifacts['verification']['verdict'] == 'fail'


def test_operation_summary_requires_evidence_links():
    from factory.control.operation_presets import operation_results
    ledger = {'items': [{'id': 'health', 'status': 'unverified'}]}
    verdict = {'operation_results': {
        'health': {'status': 'pass', 'evidence': 'unsupported pass', 'criterion_ids': ['health']},
        'startup_command': {'status': 'pass', 'evidence': 'npm start', 'criterion_ids': ['invented']}}}
    result = operation_results(verdict, ledger)
    assert result['health']['status'] == 'unverified'
    assert 'startup_command' not in result


def test_schedule_api_requires_admin_and_default_is_off(app_env):
    client, store, service, repo = app_env
    headers = login(client); p = project(client, repo, headers)
    route = f"/api/v2/projects/{p['id']}/inspection"
    assert client.get(route).json()['enabled'] is False
    response = client.put(route, json={'enabled': False, 'interval_s': 300, 'revision': 0}, headers=headers)
    assert response.status_code == 200
    assert client.put(route, json={'enabled': False, 'interval_s': 300, 'revision': 0}, headers=headers).status_code == 409
    client.post('/api/auth/logout', headers=headers)
    assert client.put(route, json={'enabled': True, 'interval_s': 300, 'revision': 1}, headers=headers).status_code == 401


def test_two_ticks_cannot_duplicate_due_inspection(app_env):
    from concurrent.futures import ThreadPoolExecutor
    service, store, p, config = schedule(app_env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.inspections.tick(config['next_at']), range(2)))
    assert sum(map(len, results)) == 1


def test_fresh_verifier_browser_observation_resolves_old_environment_failure(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    service.store.append(run['id'], 'browser.observed', {'ok': False, 'error_type': 'browser_unavailable', 'error': 'missing'}, 'worker')
    def reviewer(request, emit, cancel=None):
        emit('browser.observed', {'ok': True, 'action': 'open', 'errors': []})
        return ProviderResult(passing_review(request, '新浏览器检查通过'), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert artifacts['verification']['verdict'] == 'pass'


def test_member_cannot_enable_inspection(app_env):
    client, store, service, repo = app_env
    headers = login(client); p = project(client, repo, headers)
    client.app.state.auth.create_user('inspection-member', 'a-long-member-password', role='member')
    response = client.post('/api/auth/login', json={'username': 'inspection-member', 'password': 'a-long-member-password'}, headers={'Origin': 'http://testserver'})
    headers = {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}
    assert client.put(f"/api/v2/projects/{p['id']}/inspection", json={'enabled': True, 'interval_s': 300, 'revision': 0}, headers=headers).status_code == 403


def test_unverified_is_distinct_in_engineering_projection():
    from factory.control.engineering_overview import current_evidence
    assert current_evidence({'request': 'x', 'artifacts': {'verification': {'verdict': 'unverified'}, 'checks': [{'exit': 0}]}})['checks'] == 'unverified'


def test_browser_outage_does_not_hide_workers_real_browser_failure(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    service.store.append(run['id'], 'browser.observed', {'ok': False, 'error': 'Submit failed'}, 'worker')
    def reviewer(request, emit, cancel=None):
        emit('browser.observed', {'ok': False, 'error_type': 'browser_unavailable', 'error': 'timeout'})
        return ProviderResult(passing_review(request, 'other checks pass'), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError): service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert artifacts['verification']['verdict'] == 'fail'


def test_recovered_browser_preserves_mixed_observation_review_ids(app_env, monkeypatch):
    from factory.control.verification_evidence import browser_evidence
    service, run, p, repo = setup_review(app_env)
    service.store.append(run['id'], 'browser.observed', {'ok': True, 'errors': []}, 'worker-good')
    service.store.append(run['id'], 'browser.observed', {'ok': False, 'error_type': 'browser_unavailable', 'error': 'missing'}, 'worker-unavailable')
    ids = [o['event_id'] for o in browser_evidence(service.store, run['id'])['latest']]
    def reviewer(request, emit, cancel=None):
        emit('browser.observed', {'ok': True, 'action': 'open', 'errors': []})
        verdict = json.loads(passing_review(request, 'environment recovered; live browser checked'))
        verdict['browser_review'] = {'event_ids': ids, 'disposition': 'clean', 'reason': 'fresh observation'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', reviewer)
    artifacts = {'worktree': str(repo)}
    service._independent_verify(run['id'], run, p, service.runtime_settings.get(), artifacts)
    assert artifacts['verification']['verdict'] == 'pass'


def test_inspection_failure_superseded_preserves_evidence(app_env, monkeypatch):
    from factory.control.autonomy_routes import _attention
    service, store, p, config = schedule(app_env)
    first = service.inspections.tick(config['next_at'])[0]
    evidence = {'verification': {'verdict': 'fail', 'reason': 'health endpoint failed'}}
    def failed(rid, run, project, settings, artifacts):
        artifacts.update(evidence)
        raise ExecutionError('health endpoint failed', artifacts=artifacts)
    monkeypatch.setattr(service, '_independent_verify', failed)
    inspect_run(service, first)
    old = store.get(first)
    assert old['status'] == 'inspection_failed'
    assert old['artifacts']['verification'] == evidence['verification']
    assert _attention(old, {})['reason'] == 'health endpoint failed'
    second = service.inspections.tick(config['next_at'] + 300)[0]
    old = store.get(first)
    assert old['inspection_superseded_by'] == second
    assert old['artifacts']['verification'] == evidence['verification']
    assert old['error'] == 'health endpoint failed'
    assert _attention(old, {}) is None
    inspect_run(service, second)
    assert _attention(store.get(second), {}, {p['id']: second}) is not None
    assert _attention(store.get(second), {}, {p['id']: 'newer'}) is None


def test_operation_store_reused_under_concurrent_submissions(app_env, monkeypatch):
    from contextlib import contextmanager
    from concurrent.futures import ThreadPoolExecutor
    client, store, service, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    monkeypatch.setattr(service, 'start_plan', lambda rid: None)
    original = store.connect
    statements = []
    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, 'connect', traced)
    def submit(index):
        return client.post('/api/v2/runs', headers=headers, json={
            'project_id': pid, 'request': '保存失败', 'operation': 'bugfix',
            'idempotency_key': f'concurrent-operation-{index}',
        })
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert all(response.status_code == 201 for response in responses)
    assert client.get('/api/v2/operation-presets').status_code == 200
    service.inspections.tick()
    assert service.inspections.operations is service.operations
    assert not any('CREATE TABLE' in sql.upper() for sql in statements)
    assert not any('INSERT OR IGNORE INTO OPERATION_PRESETS' in sql.upper() for sql in statements)


def test_legacy_inspections_latest_only_and_atomic_supersession(app_env, monkeypatch):
    from factory.control.autonomy_routes import overview
    service, store, p, config = schedule(app_env)
    first = service.inspections.tick(config['next_at'])[0]
    store.update(first, {'status': 'needs_human', 'artifacts': {'verification': {'reason': 'legacy failure'}}},
                 event=('run.failed', {'message': 'legacy failure'}))
    original_event = store._event
    def broken(db, rid, kind, *args, **kwargs):
        if kind == 'inspection.enqueued':
            raise RuntimeError('simulated queue transaction failure')
        return original_event(db, rid, kind, *args, **kwargs)
    monkeypatch.setattr(store, '_event', broken)
    with pytest.raises(RuntimeError):
        service.inspections.tick(config['next_at'] + 300)
    assert not store.get(first).get('inspection_superseded_by')
    assert service.inspections.get(p['id'])['last_run_id'] == first
    monkeypatch.setattr(store, '_event', original_event)
    second = service.inspections.tick(config['next_at'] + 300)[0]
    assert store.get(first)['error'] == 'legacy failure'
    assert store.get(first)['status'] == 'inspection_failed'
    store.update(second, {'status': 'inspection_failed', 'error': 'latest failure'})
    result = overview(store, project_id=p['id'])
    assert [item['id'] for item in result['attention']] == [second]
    assert result['attention_runs'] == result['project_summaries'][0]['attention_runs'] == 1
    store.update(second, {'status': 'inspection_completed'})
    assert overview(store, project_id=p['id'])['attention'] == []


@pytest.mark.parametrize('status', ['received', 'verifying'])
def test_interrupted_inspection_never_becomes_needs_human(app_env, monkeypatch, status):
    service, store, p, config = schedule(app_env)
    rid = service.inspections.tick(config['next_at'])[0]
    store.update(rid, {'status': status, 'policy': {'resume_on_restart': False},
                       'artifacts': {'verification': {'reason': 'partial evidence'}}})
    monkeypatch.setattr(service, '_ensure_scheduler', lambda: None)
    service.recover()
    failed = store.get(rid)
    assert failed['status'] == 'inspection_failed'
    assert '中断' in failed['error']
    assert failed['artifacts']['verification']['reason'] == 'partial evidence'
