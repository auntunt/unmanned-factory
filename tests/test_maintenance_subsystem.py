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
    # The superseded revision still delivered: it stays counted and visible.
    ov = client.get('/api/v2/maintenance/overview', headers=headers).json()
    assert ov['counts']['delivered_in_window'] == 1
    graph = client.get('/api/v2/maintenance/graph', headers=headers).json()
    assert any(n['id'].startswith(f'artifact:{task_id}:') for n in graph['nodes'])

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


def test_dirty_workspace_is_a_need_and_a_pre_plan_block_continues_as_a_revision(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    (repo / 'scratch.log').write_text('untracked')
    view = _register(client, repo, headers)
    assert '清理工作区的未提交/未跟踪改动' in view['needs']
    assert {f['id']: f['status'] for f in view['probe']['findings']}['clean'] == 'failed'
    pid = view['project_id']
    _adopt_greeting_check(store, pid)
    receipt = client.post('/api/v2/maintenance/requirements', json={
        'project_id': pid, 'content': '把问候语改成 hello world', 'idempotency_key': 'dirty-key-0001'},
        headers=headers).json()
    wait_state(store, receipt['execution_id'], {'needs_human'})
    task = client.get(f"/api/v2/maintenance/tasks/{receipt['task_id']}", headers=headers).json()
    assert 'feedback' in task['actions'] and 'resume' not in task['actions']
    (repo / 'scratch.log').unlink()
    fb = client.post(f"/api/v2/maintenance/tasks/{receipt['task_id']}/feedback",
                     json={'content': '工作区已清理，请重新开始'}, headers=headers)
    assert fb.status_code == 201, fb.text
    assert fb.json()['predecessor_id'] == receipt['task_id']
    old = client.get(f"/api/v2/maintenance/tasks/{receipt['task_id']}", headers=headers).json()
    assert old['status'] == 'cancelled'
    wait_state(store, fb.json()['execution_id'], {'awaiting_approval'})


def test_generated_task_text_does_not_trip_the_high_risk_triage_words(app_env):
    """Only the requirement itself may make a task high-risk, not our template."""
    client, store, svc, repo = app_env
    from factory.control.planning import _HIGH_RISK_RE
    headers = login(client)
    pid = _register(client, repo, headers)['project_id']
    core = client.app.state.maintenance
    record = {'id': 'r' * 32, 'title': '修复金额', 'content': '修复金额', 'attachments': [],
              'external_id': None, 'source': {'kind': 'manual', 'name': 'owner'}}
    request = core._task_request(record, store.project(pid), key='k' * 12)
    template = ' '.join([request['expected_behaviour'], request['delivery_goal']])
    assert not _HIGH_RISK_RE.search(template), _HIGH_RISK_RE.search(template)


class _ChainSDK:
    """Deterministic executor: round A fixes the greeting and adds a.txt, any later
    round adds b.txt. Which round it is comes from the working copy it is handed,
    so the test proves which baseline the executor really received."""

    def available(self):
        return [{'id': 'codex', 'installed': True, 'detail': 'test double'}]

    def run(self, request, emit, cancel=None):
        import json
        from pathlib import Path
        from factory.control.providers import ProviderResult
        root = Path(request.workspace)
        later = (root / 'a.txt').exists()
        target = 'b.txt' if later else 'a.txt'
        if request.read_only:
            paths = [target] if later else ['greeting.txt', target]
            plan = {'title': 'change', 'summary': 'deterministic change', 'questions': [],
                    'tasks': [{'id': 'change', 'title': 'change', 'prompt': 'change',
                               'acceptance': ['done'], 'paths': paths, 'checks': ['greeting'],
                               'depends_on': [], 'complexity': 'small', 'risk': 'low'}]}
            return ProviderResult(json.dumps(plan), cost_usd=0.0)
        if not later:
            (root / 'greeting.txt').write_text('hello world')
        (root / target).write_text(target)
        return ProviderResult('done', cost_usd=0.0)


def _deliver(client, store, task_id, execution_id, headers):
    wait_state(store, execution_id, {'awaiting_approval', 'ready_for_review'})
    if store.get(execution_id)['status'] == 'awaiting_approval':
        assert client.post(f'/api/v2/maintenance/tasks/{task_id}/approve', headers=headers).status_code == 200
    wait_state(store, execution_id, {'ready_for_review'})


def test_feedback_after_delivery_keeps_the_delivered_change_and_states_patch_basis(app_env):
    client, store, svc, repo = app_env
    svc.runner = _ChainSDK()
    headers = login(client)
    pid = _register(client, repo, headers, name='Chain')['project_id']
    _adopt_greeting_check(store, pid)
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    first = client.post('/api/v2/maintenance/requirements', json={
        'project_id': pid, 'content': '改 A', 'idempotency_key': 'chain-key-0001'}, headers=headers).json()
    _deliver(client, store, first['task_id'], first['execution_id'], headers)
    delivered_a = store.get(first['execution_id'])['artifacts']['commit']

    fb = client.post(f"/api/v2/maintenance/tasks/{first['task_id']}/feedback",
                     json={'content': '再加 B'}, headers=headers)
    assert fb.status_code == 201, fb.text
    second = fb.json()
    assert second['baseline']['base_sha'] == delivered_a  # continues from A, not the old baseline
    assert store.get(second['execution_id'])['feedback_predecessor_id'] == first['execution_id']
    _deliver(client, store, second['task_id'], second['execution_id'], headers)

    # The final tree holds both A and B; the user's own branch was not touched.
    final = store.get(second['execution_id'])['artifacts']
    tree = subprocess.check_output(['git', 'ls-tree', '--name-only', final['commit']], cwd=repo, text=True)
    assert {'a.txt', 'b.txt'} <= set(tree.split())
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip() == base

    exported = client.get(f"/api/v2/maintenance/tasks/{second['task_id']}/export", headers=headers).json()
    basis = {b['kind']: b for b in exported['receipt']['delivery']['patch_basis']}
    assert basis['incremental']['applies_to'] == delivered_a
    assert basis['cumulative']['applies_to'] == base
    incremental = client.get(f"/api/v2/maintenance/tasks/{second['task_id']}/artifacts/"
                             f"{basis['incremental']['artifact']}", headers=headers).text
    cumulative = client.get(f"/api/v2/maintenance/tasks/{second['task_id']}/artifacts/"
                            f"{basis['cumulative']['artifact']}", headers=headers).text
    assert 'b/b.txt' in incremental and 'b/a.txt' not in incremental
    assert 'b/a.txt' in cumulative and 'b/b.txt' in cumulative
    assert '应用于' in exported['text']


def test_feedback_refuses_rather_than_redo_when_the_delivery_is_gone(app_env):
    import shutil
    client, store, svc, repo = app_env
    svc.runner = _ChainSDK()
    headers = login(client)
    pid = _register(client, repo, headers, name='Chain')['project_id']
    _adopt_greeting_check(store, pid)
    first = client.post('/api/v2/maintenance/requirements', json={
        'project_id': pid, 'content': '改 A', 'idempotency_key': 'chain-key-0002'}, headers=headers).json()
    _deliver(client, store, first['task_id'], first['execution_id'], headers)
    shutil.rmtree(store.get(first['execution_id'])['artifacts']['worktree'])
    fb = client.post(f"/api/v2/maintenance/tasks/{first['task_id']}/feedback",
                     json={'content': '再加 B'}, headers=headers)
    assert fb.status_code == 409 and '不会退回旧基线' in fb.json()['detail']
    old = client.get(f"/api/v2/maintenance/tasks/{first['task_id']}", headers=headers).json()
    assert old['successor_id'] is None


def test_synthetic_is_declared_explicitly_and_travels_to_task_revision_and_receipt(app_env):
    client, store, svc, repo = app_env
    svc.runner = _ChainSDK()
    headers = login(client)
    view = client.post('/api/v2/maintenance/repos', json={'source': str(repo), 'name': 'Demo', 'synthetic': True},
                       headers=headers).json()
    assert view['synthetic'] is True
    pid = view['project_id']
    _adopt_greeting_check(store, pid)
    receipt = client.post('/api/v2/maintenance/requirements', json={
        'project_id': pid, 'content': '改 A', 'idempotency_key': 'synth-key-0001'}, headers=headers).json()
    assert receipt['synthetic'] is True
    _deliver(client, store, receipt['task_id'], receipt['execution_id'], headers)
    task = client.get(f"/api/v2/maintenance/tasks/{receipt['task_id']}", headers=headers).json()
    assert task['synthetic'] is True
    exported = client.get(f"/api/v2/maintenance/tasks/{receipt['task_id']}/export", headers=headers).json()
    assert exported['receipt']['synthetic'] is True
    successor = client.post(f"/api/v2/maintenance/tasks/{receipt['task_id']}/feedback",
                            json={'content': '再加 B'}, headers=headers).json()
    assert successor['synthetic'] is True
    graph = client.get('/api/v2/maintenance/graph', headers=headers).json()
    assert all(n.get('synthetic') for n in graph['nodes'] if n['type'] in ('repo', 'requirement', 'task'))
    # Undeclared repositories stay real by default.
    client.post(f'/api/v2/maintenance/repos/{pid}/synthetic', json={'synthetic': False}, headers=headers)
    assert client.get('/api/v2/maintenance/repos', headers=headers).json()['repos'][0]['synthetic'] is False


def test_probe_flags_a_python3_that_cannot_import_pytest(tmp_path, monkeypatch):
    from factory.control.maintenance_subsystem import _check_runnable
    fake = tmp_path / 'bin'
    fake.mkdir()
    (fake / 'python3').write_text('#!/bin/sh\nexit 1\n')
    (fake / 'python3').chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake}:/usr/bin:/bin')
    ok, note = _check_runnable(['python3', '-m', 'pytest', '-q'], tmp_path)
    assert ok is False and '没有安装 pytest' in note


def test_docker_python_probe_does_not_require_host_pytest(app_env, tmp_path, monkeypatch):
    from factory.control.maintenance_subsystem import detect_stack
    client, store, svc, repo = app_env
    (repo / 'requirements.txt').write_text('pytest\n')
    (repo / 'Dockerfile').write_text('FROM python:3.12\n')
    (repo / 'tests').mkdir()
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'docker test fixtures'], cwd=repo, check=True)
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    docker = fake_bin / 'docker'
    docker.write_text('#!/bin/sh\n[ "$1" = info ]\n')
    docker.chmod(0o755)
    python3 = fake_bin / 'python3'
    python3.write_text('#!/bin/sh\nexit 1\n')
    python3.chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake_bin}:/usr/bin:/bin')
    _, suggestions = detect_stack(repo)
    assert suggestions[0]['argv'] == ['@dockerfile', 'python3', '-m', 'pytest', '-q', 'tests']
    view = _register(client, repo, login(client))
    old = store.project(view['project_id'])
    store.update_project(view['project_id'], {'checks': {'pytest': ['python3', '-m', 'pytest', '-q']}},
                         old['revision'], 'test')
    view = client.post(f"/api/v2/maintenance/repos/{view['project_id']}/probe",
                       headers=login(client)).json()
    assert view['state'] == 'needs_input'
    assert '将 pytest 检查切换到 Docker 容器' in view['needs']
    pytest_suggestion = next(c for c in view['probe']['suggested_checks'] if c['name'] == 'pytest')
    assert pytest_suggestion['available'] is True
    assert 'Docker 容器内' in next(f['message'] for f in view['probe']['findings']
                                  if f['id'] == 'suggest:pytest')
    adopted = client.post(f"/api/v2/maintenance/repos/{view['project_id']}/checks",
                          json={'adopt': ['pytest']}, headers=login(client))
    assert adopted.status_code == 200, adopted.text
    assert store.project(view['project_id'])['checks']['pytest'][0] == '@dockerfile'
    assert adopted.json()['state'] == 'ready'
    readiness = client.get(f"/api/v2/projects/{view['project_id']}/readiness",
                           headers=login(client)).json()
    assert any(c['id'] == 'check:pytest' and c['status'] == 'ok' for c in readiness['checks'])
    docker.write_text('#!/bin/sh\nexit 1\n')
    readiness = client.get(f"/api/v2/projects/{view['project_id']}/readiness",
                           headers=login(client)).json()
    assert any(c['id'] == 'check:pytest' and c['status'] == 'blocked' for c in readiness['checks'])


def test_docker_python_probe_fails_closed_without_docker(tmp_path, monkeypatch):
    from factory.control.maintenance_subsystem import _check_runnable
    (tmp_path / 'Dockerfile').write_text('FROM python:3.12\n')
    empty = tmp_path / 'empty-bin'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    ok, note = _check_runnable(['@dockerfile', 'python3', '-m', 'pytest', '-q'], tmp_path)
    assert ok is False and '不会退回宿主机' in note


def _member(client, pid, name):
    user = client.app.state.auth.create_user(name, 'long-member-password', role='member')
    if pid:
        client.app.state.governance.assign(user['id'], [pid], 'owner')
    client.cookies.clear()
    response = client.post('/api/auth/login', json={'username': name, 'password': 'long-member-password'},
                           headers={'Origin': 'http://testserver'})
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}


def test_member_own_task_can_cancel(app_env):
    """Codex review e2d87cf: actions offered cancel, the gateway answered 403."""
    client, store, svc, repo = app_env
    headers = login(client)
    pid = _register(client, repo, headers)['project_id']
    headers = _member(client, pid, 'member-check')
    response = client.post('/api/v2/maintenance/requirements', json={'project_id': pid, 'content': 'Fix greeting'},
                           headers=headers)
    assert response.status_code == 201, response.text
    tid = response.json()['task_id']
    view = client.get(f'/api/v2/maintenance/tasks/{tid}').json()
    assert 'cancel' in view['actions'], view
    response = client.post(f'/api/v2/maintenance/tasks/{tid}/cancel', headers=headers)
    assert response.status_code == 200, response.text


def test_member_cannot_act_on_someone_elses_task_and_is_not_offered_to(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = _register(client, repo, headers)['project_id']
    theirs = client.post('/api/v2/maintenance/requirements', json={
        'project_id': pid, 'content': 'admin task', 'idempotency_key': 'admin-task-0001'}, headers=headers).json()
    headers = _member(client, pid, 'member-other')
    view = client.get(f"/api/v2/maintenance/tasks/{theirs['task_id']}").json()
    assert not ({'cancel', 'approve', 'answer', 'supplement', 'resume', 'feedback'} & set(view['actions']))
    for action in ('cancel', 'approve', 'resume', 'feedback'):
        res = client.post(f"/api/v2/maintenance/tasks/{theirs['task_id']}/{action}", json={'content': 'x'}, headers=headers)
        assert res.status_code == 403, (action, res.text)
    # Admin-only entry points stay admin-only for members.
    assert client.post('/api/v2/maintenance/repos', json={'source': str(repo), 'name': 'x'}, headers=headers).status_code == 403
    assert client.post(f"/api/v2/maintenance/requirements/{theirs['requirement_id']}/dispatch", headers=headers).status_code == 403
    assert client.post('/api/v2/maintenance/intake-sources', json={'name': 'n', 'project_ids': [pid]},
                       headers=headers).status_code == 403


def test_member_without_the_project_cannot_submit_or_act(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = _register(client, repo, headers)['project_id']
    headers = _member(client, None, 'member-outside')
    res = client.post('/api/v2/maintenance/requirements', json={'project_id': pid, 'content': 'x'}, headers=headers)
    assert res.status_code == 403, res.text
