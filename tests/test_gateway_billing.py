import pytest
from fastapi.testclient import TestClient
from tests.test_runtime_execution import ServiceRunner, Publisher, _app_env, _plan, _task, _login, _create_project, _update_runtime, _wait

@pytest.mark.parametrize('worker_cost', [None, 100.0])
def test_gateway_owns_billing_through_publish(tmp_path, worker_cost):
    runner = ServiceRunner(plans=[_plan(_task('greeting'))], worker_cost=worker_cost)
    publisher = Publisher()
    app, store, service, repo = _app_env(tmp_path, runner, publisher=publisher)
    with TestClient(app) as client:
        headers = _login(client)
        p = _create_project(client, repo, headers, auto_publish=True)
        _update_runtime(service, prefix='gateway', unknown_cost_policy='stop')
        service.governance.set_limit('workspace', 'all', 0, 'test')
        from factory.control.autonomy import DEFAULT_POLICY
        service.policies.update(p['id'], {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'test')
        rid = client.post('/api/v2/runs', headers=headers, json={'project_id': p['id'], 'request': 'greeting'}).json()['id']
        planned = _wait(store, rid, {'awaiting_approval'})
        assert client.post(f'/api/v2/runs/{rid}/approve', headers=headers, json={'revision': planned['revision']}).status_code == 200
        run = _wait(store, rid, {'published'})
        assert publisher.calls == 1
        assert run['status'] == 'published'
        assert client.get(f'/api/v3/runs/{rid}/deliverables/download').status_code == 200


def test_paused_run_reports_current_failure_over_historical_billing(tmp_path):
    app, store, service, repo = _app_env(tmp_path, ServiceRunner(plans=[]))
    with TestClient(app) as client:
        headers = _login(client)
        p = _create_project(client, repo, headers)
        run, _ = store.create_run(p['id'], 'test', source={'type': 'test'})
        rid = run['id']
        store.update(rid, {'status': 'needs_human', 'artifacts': {'needs_human': 'cost unknown'}}, event=('run.failed', {'message': 'task timeout after 600s'}))
        assert store.get(rid)['error'] == 'task timeout after 600s'
        assert next(r for r in store.runs() if r['id'] == rid)['error'] == 'task timeout after 600s'
        store.append(rid, 'run.started', {})
        assert 'error' not in store.get(rid)
