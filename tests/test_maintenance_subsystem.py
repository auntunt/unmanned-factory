"""Maintenance subsystem: repo registration, one intake for humans and machines,
monitor/graph projections over the same objects, backend-driven task actions.

The execution uses the same FakeSDK the control-app tests use (synthetic, no paid
model): it plans, waits for approval, edits greeting.txt and passes the check.
"""
import subprocess
import time

from tests.test_control_app import app_env, login, wait_state  # noqa: F401


def _register(client, repo, headers, name='Sample'):
    res = client.post('/api/v2/maintenance/repos', json={'source': str(repo), 'name': name}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _adopt_greeting_check(store, pid):
    import sys
    project = store.project(pid)
    store.update_project(pid, {'checks': {'greeting': [sys.executable, '-c',
        "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello world'"]}},
        project['revision'], 'test')


def test_register_local_repo_probes_and_reuses_project(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    (repo / 'package.json').write_text('{"scripts": {"test": "node t.js"}}')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'pkg'], cwd=repo, check=True)
    view = _register(client, repo, headers)
    assert view['state'] == 'needs_input' and view['state_label'] == '待补充'
    assert view['repository'].startswith('local/')
    assert any(s['name'] == 'Node.js' for s in view['probe']['stack'])
    assert [c['name'] for c in view['probe']['suggested_checks']] == ['test']
    statuses = {f['id']: f['status'] for f in view['probe']['findings']}
    assert statuses['access'] == 'verified' and statuses['checks'] == 'missing'
    assert statuses['stack:Node.js'] == 'found'  # discovered is not verified
    # Same directory again: the existing project id comes back, no second project.
    again = _register(client, repo, headers, name='Other name')
    assert again['project_id'] == view['project_id'] and again['reused'] is True
    assert len(store.projects()) == 1
    adopted = client.post(f"/api/v2/maintenance/repos/{view['project_id']}/checks",
                          json={'adopt': ['test']}, headers=headers)
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()['state'] == 'ready' and adopted.json()['checks_configured'] == ['test']


def test_register_refuses_paths_outside_workspace_root(app_env, tmp_path):
    client, store, svc, repo = app_env
    headers = login(client)
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    res = client.post('/api/v2/maintenance/repos', json={'source': str(outside), 'name': 'x'}, headers=headers)
    assert res.status_code == 422 and 'FACTORY_WORKSPACE_ROOT' in res.json()['detail']
    res = client.post('/api/v2/maintenance/repos', json={'source': str(repo), 'name': 'x',
                                                         'credential_ref': 'ghp_abc token'}, headers=headers)
    assert res.status_code == 422


def test_manual_and_machine_intake_share_one_path_to_a_delivered_task(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    view = _register(client, repo, headers)
    pid = view['project_id']
    _adopt_greeting_check(store, pid)

    # Manual: short text, no SHA/JSON; idempotent per client key.
    body = {'project_id': pid, 'content': '把问候语改成 hello world', 'idempotency_key': 'manual-key-0001'}
    first = client.post('/api/v2/maintenance/requirements', json=body, headers=headers)
    assert first.status_code == 201, first.text
    receipt = first.json()
    assert receipt['status'] == 'dispatched' and receipt['task_id'] and not receipt['duplicate']
    assert receipt['source'] == {'kind': 'manual', 'name': 'owner'}
    dup = client.post('/api/v2/maintenance/requirements', json=body, headers=headers)
    assert dup.status_code == 200 and dup.json()['duplicate'] is True
    assert dup.json()['requirement_id'] == receipt['requirement_id']
    changed = client.post('/api/v2/maintenance/requirements', json={**body, 'content': '别的内容'}, headers=headers)
    assert changed.status_code == 409

    # Machine: token-scoped, no session, source fields in the body are ignored.
    src = client.post('/api/v2/maintenance/intake-sources',
                      json={'name': 'alerts', 'project_ids': [pid], 'auto_dispatch': False}, headers=headers)
    assert src.status_code == 201, src.text
    token = src.json()['token']
    assert token.startswith('wbm_') and token not in str(client.get('/api/v2/maintenance/intake-sources').json())
    client.cookies.clear()
    machine = {'project_id': pid, 'content': '告警：问候语不对', 'external_id': 'ALERT-7',
               'source': 'admin', 'role': 'admin'}
    assert client.post('/api/v2/maintenance/intake', json=machine).status_code == 401
    bad = client.post('/api/v2/maintenance/intake', json={**machine, 'project_id': 'nope'},
                      headers={'Authorization': f'Bearer {token}'})
    assert bad.status_code == 403
    got = client.post('/api/v2/maintenance/intake', json=machine, headers={'Authorization': f'Bearer {token}'})
    assert got.status_code == 201, got.text
    auto = got.json()
    assert auto['status'] == 'pending_dispatch' and auto['task_id'] is None
    assert auto['source'] == {'kind': 'api', 'name': 'alerts'}
    again = client.post('/api/v2/maintenance/intake', json=machine, headers={'Authorization': f'Bearer {token}'})
    assert again.status_code == 200 and again.json()['duplicate']
    headers = login(client)

    # Monitor sees both, deduplicated per task; the pending one is not a task yet.
    task_id = receipt['task_id']
    run_id = receipt['execution_id']
    wait_state(store, run_id, {'awaiting_approval'})
    ov = client.get('/api/v2/maintenance/overview', headers=headers).json()
    assert ov['counts']['projects'] == 1
    assert ov['counts']['attention'] == {'total': 1, 'answer': 0, 'approval': 1, 'blocked': 0}
    assert ov['attention'][0]['task_id'] == task_id
    assert ov['sources']['server_metrics']['status'] == 'not_connected'
    assert ov['window']['timezone']
    task = client.get(f'/api/v2/maintenance/tasks/{task_id}', headers=headers).json()
    assert 'approve' in task['actions'] and 'pause' not in task['actions']

    graph = client.get('/api/v2/maintenance/graph', headers=headers).json()
    ids = {n['id'] for n in graph['nodes']}
    assert {f'repo:{pid}', f"req:{receipt['requirement_id']}", f"req:{auto['requirement_id']}",
            f'task:{task_id}', f'target:{task_id}', f'blocker:{task_id}'} <= ids
    assert not any(n['type'] == 'artifact' for n in graph['nodes'])

    # Approve through the existing gate; the executor delivers a real patch.
    ok = client.post(f'/api/v2/maintenance/tasks/{task_id}/approve', headers=headers)
    assert ok.status_code == 200, ok.text
    wait_state(store, run_id, {'ready_for_review'})
    task = client.get(f'/api/v2/maintenance/tasks/{task_id}', headers=headers).json()
    assert task['status'] == 'delivered' and {'export', 'feedback'} <= set(task['actions'])
    ov = client.get('/api/v2/maintenance/overview', headers=headers).json()
    assert ov['counts']['delivered_in_window'] == 1 and ov['counts']['attention']['total'] == 0
    graph = client.get('/api/v2/maintenance/graph', headers=headers).json()
    assert any(n['id'].startswith(f'artifact:{task_id}:') for n in graph['nodes'])
    assert f'target:{task_id}' not in {n['id'] for n in graph['nodes']}

    # Follow-up feedback becomes a linked revision, not an in-place resume.
    fb = client.post(f'/api/v2/maintenance/tasks/{task_id}/feedback',
                     json={'content': '再加一个感叹号'}, headers=headers)
    assert fb.status_code == 201, fb.text
    successor = fb.json()
    assert successor['predecessor_id'] == task_id and successor['revision'] == 2
    assert '后续反馈' in successor['issue']['body']
    old = client.get(f'/api/v2/maintenance/tasks/{task_id}', headers=headers).json()
    assert old['successor_id'] == successor['task_id'] and 'feedback' not in old['actions']

    # The human dispatches the machine requirement explicitly.
    sent = client.post(f"/api/v2/maintenance/requirements/{auto['requirement_id']}/dispatch", headers=headers)
    assert sent.status_code == 200 and sent.json()['status'] == 'dispatched' and sent.json()['task_id']
    reqs = client.get('/api/v2/maintenance/requirements', headers=headers).json()['requirements']
    assert {r['source']['kind'] for r in reqs} == {'manual', 'api'}
    for rid in {successor['execution_id'], sent.json()['execution_id']}:
        wait_state(store, rid, {'awaiting_approval'})


def test_stopped_plugin_refuses_intake_and_revoked_token_is_refused(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = _register(client, repo, headers)['project_id']
    src = client.post('/api/v2/maintenance/intake-sources',
                      json={'name': 'ci', 'project_ids': [pid]}, headers=headers).json()
    from factory.control.plugins import PluginAvailability
    availability = PluginAvailability(store)
    availability.set_state('issue-maintenance', 'draining', actor='test', active_probe=lambda: 0)
    availability.set_state('issue-maintenance', 'disabled', actor='test', active_probe=lambda: 0)
    res = client.post('/api/v2/maintenance/requirements',
                      json={'project_id': pid, 'content': 'x', 'idempotency_key': 'stopped-0001'}, headers=headers)
    assert res.status_code == 409
    assert client.get('/api/v2/maintenance/requirements', headers=headers).json()['requirements'] == []
    client.post(f"/api/v2/maintenance/intake-sources/{src['id']}/revoke", headers=headers)
    client.cookies.clear()
    res = client.post('/api/v2/maintenance/intake', json={'project_id': pid, 'content': 'x', 'external_id': 'e1'},
                      headers={'Authorization': f"Bearer {src['token']}"})
    assert res.status_code == 401


def test_overview_reports_executor_and_empty_scope_honestly(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    ov = client.get('/api/v2/maintenance/overview', headers=headers).json()
    assert ov['counts']['projects'] == 0 and ov['attention'] == []
    assert ov['sources']['executor']['status'] in ('online', 'offline', 'unknown')
    manifest = client.get('/api/v2/maintenance/manifest', headers=headers).json()
    assert manifest['supports_pause'] is False and manifest['contract_version'] == 'maintenance-subsystem/1'
