"""Maintenance-task HTTP surface: create, read, intervene, cancel, export, download,
and the direct refusal edges (unauthenticated, wrong project, unknown task, malformed).
"""
import json
import subprocess
import uuid

from factory.control.issue_maintenance import (
    MaintenanceStore, normalize, content_fingerprint,
)
from factory.control.issue_maintenance_webuddy import SOURCE_TYPE
from tests.test_control_app import app_env, login, project  # noqa: F401


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _base_sha(repo):
    return subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, text=True,
    ).strip()


def _valid_request(project_dict, base_sha, *, key=None):
    """A minimal valid maintenance request body."""
    return {
        'issue': {
            'source': 'test', 'external_id': '42', 'version': '1',
            'title': 'Something broke', 'body': 'Steps to reproduce...',
        },
        'project_id': project_dict['id'],
        'repository': project_dict['repository'],
        'base_sha': base_sha,
        'expected_behaviour': 'It should not break',
        'delivery_goal': 'Fix and verify',
        'agreement': {'revision': 'v1'},
        'idempotency_key': key or f'test-{uuid.uuid4().hex[:16]}',
    }


def _controlled_task(client, store, repo, headers, execution_status='received',
                     *, artifacts=None, base_sha=None):
    """A maintenance task parked in a deterministic state.

    Creates the run via ``store.create_run`` (no scheduler), then builds the
    maintenance record directly in the module's own tables.  Nothing is
    dispatched, so no background thread can race the test.
    """
    p = project(client, repo, headers)
    user = client.get('/api/auth/me', headers=headers).json()['user']
    sha = base_sha or _base_sha(repo)
    task_id = uuid.uuid4().hex
    idem_key = f'ctrl-{uuid.uuid4().hex[:12]}'

    run, _ = store.create_run(
        p['id'], 'maintenance test prompt',
        source={'type': SOURCE_TYPE, 'actor': user['username'],
                'actor_id': user['id'], 'maintenance_task_id': task_id,
                'repository': p['repository']},
        delivery_id=f'maintenance:{task_id}')
    changes = {'status': execution_status}
    if artifacts:
        changes['artifacts'] = artifacts
    store.update(run['id'], changes)

    mstore = MaintenanceStore(store)
    normalized = normalize({
        'issue': {'source': 'test', 'external_id': '1', 'version': '1',
                  'title': 'Test issue', 'body': 'Test body'},
        'project_id': p['id'], 'repository': p['repository'],
        'base_sha': sha, 'expected_behaviour': 'Works',
        'delivery_goal': 'Fix', 'agreement': {'revision': '1'},
        'idempotency_key': idem_key,
    })
    fp = content_fingerprint(normalized)
    record, _ = mstore.claim(f'maintenance:{p["id"]}:{idem_key}', normalized, fp)
    mstore.link(record['id'], {'execution_id': run['id']})
    return record['id'], p, run['id'], sha


# ---------------------------------------------------------------------------
# create + read back
# ---------------------------------------------------------------------------

def test_create_and_read_back(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    sha = _base_sha(repo)
    body = _valid_request(p, sha)

    res = client.post('/api/v2/maintenance/tasks', json=body, headers=headers)
    assert res.status_code == 201, res.text
    view = res.json()

    # Structural checks: the view carries the port's own shape
    assert view['task_id']
    assert view['project_id'] == p['id']
    assert view['issue']['title'] == body['issue']['title']
    assert view['baseline']['base_sha'] == sha
    assert view['status'] in (
        'received', 'running', 'waiting', 'cancelling',
        'delivered', 'failed', 'cancelled',
    )
    assert isinstance(view['steps'], list) and len(view['steps']) > 0
    assert view['schema_version'] == 1
    assert view['revision'] == 1

    # Read back via GET
    got = client.get(f"/api/v2/maintenance/tasks/{view['task_id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()['task_id'] == view['task_id']


# ---------------------------------------------------------------------------
# list by project
# ---------------------------------------------------------------------------

def test_list_by_project(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    tid, p, _, _ = _controlled_task(client, store, repo, headers)

    res = client.get(f"/api/v2/maintenance/tasks?project_id={p['id']}", headers=headers)
    assert res.status_code == 200
    tasks = res.json()['tasks']
    assert any(t['task_id'] == tid for t in tasks)

    # A different (non-existent) project returns an empty list or a 404/403
    # depending on governance.  The port raises for unknown projects,
    # so we just confirm the endpoint does not crash.
    fake_pid = 'nonexistent-project-id'
    res2 = client.get(f'/api/v2/maintenance/tasks?project_id={fake_pid}', headers=headers)
    assert res2.status_code in (200, 403, 404)


# ---------------------------------------------------------------------------
# follow-up returning a real receipt
# ---------------------------------------------------------------------------

def test_follow_up_returns_real_receipt(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='needs_human')

    res = client.post(
        f'/api/v2/maintenance/tasks/{tid}/follow-up',
        json={'content': 'Please also check the edge case.'},
        headers=headers)
    assert res.status_code == 200, res.text
    receipt = res.json()
    assert receipt['recorded'] is True
    assert receipt['task_id'] == tid
    assert receipt['status']  # non-empty
    # At a human gate the supplement is durably recorded and waits for the run
    # to resume. Saying "applied" here would be a receipt for work not done.
    assert receipt['applied'] is False
    assert receipt['state'] == 'pending'
    assert [s['content'] for s in receipt['supplements']] == [
        'Please also check the edge case.']

    # It is part of the task, not only of the reply to the POST, so a reader
    # arriving later (a refresh) still sees it.
    view = client.get(f'/api/v2/maintenance/tasks/{tid}', headers=headers).json()
    assert [(s['content'], s['state']) for s in view['supplements']] == [
        ('Please also check the edge case.', 'pending')]


def test_follow_up_reports_applied_once_the_run_consumed_it(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='needs_human')
    client.post(f'/api/v2/maintenance/tasks/{tid}/follow-up',
                json={'content': '补充：先修复空输入'}, headers=headers)
    pending = next(store.export_events(rid, kind='followup.pending'))['payload']

    # The run consuming the supplement is what makes it applied; this writes the
    # same receipt event the lifecycle writes.
    store.append(rid, 'followup.applied', {'pending_id': pending['id']})

    view = client.get(f'/api/v2/maintenance/tasks/{tid}', headers=headers).json()
    assert [s['state'] for s in view['supplements']] == ['applied']


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

def test_cancel(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='running')

    res = client.post(
        f'/api/v2/maintenance/tasks/{tid}/cancel', headers=headers)
    assert res.status_code == 200, res.text
    view = res.json()
    # After cancel, the status should reflect the cancellation intent.
    assert view['status'] in ('cancelling', 'cancelled')


# ---------------------------------------------------------------------------
# export + download returning real bytes
# ---------------------------------------------------------------------------

def test_export_and_download(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    sha = _base_sha(repo)

    # Create a second commit so git format-patch has a diff
    patch_file = repo / 'maintenance-fix.txt'
    patch_file.write_text('fixed content\n')
    subprocess.run(['git', 'add', 'maintenance-fix.txt'], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'maintenance fix'], cwd=repo,
                   check=True, capture_output=True,
                   env={**dict(__import__('os').environ),
                        'GIT_AUTHOR_NAME': 'Test', 'GIT_COMMITTER_NAME': 'Test',
                        'GIT_AUTHOR_EMAIL': 'test@test', 'GIT_COMMITTER_EMAIL': 'test@test'})
    commit_sha = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()

    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='ready_for_review',
        base_sha=sha,
        artifacts={
            'commit': commit_sha,
            'base_sha': sha,
            'worktree': str(repo),
            'checks': [{'name': 'greeting', 'exit': 0}],
        })

    # Export
    res = client.get(f'/api/v2/maintenance/tasks/{tid}/export', headers=headers)
    assert res.status_code == 200, res.text
    export = res.json()
    assert 'receipt' in export
    assert 'text' in export
    assert isinstance(export['artifacts'], list)
    assert len(export['artifacts']) > 0
    art = export['artifacts'][0]
    assert 'name' in art and 'size' in art
    assert isinstance(art['size'], int) and art['size'] > 0

    # Download artifact
    dl = client.get(
        f"/api/v2/maintenance/tasks/{tid}/artifacts/{art['name']}", headers=headers)
    assert dl.status_code == 200
    assert len(dl.content) == art['size']
    assert b'maintenance-fix.txt' in dl.content  # patch should mention the file
    assert 'attachment' in dl.headers.get('content-disposition', '')


def test_artifact_download_path_traversal_refused(app_env):
    """A name that does not appear in the artifact list returns 404,
    regardless of what it looks like as a filesystem path."""
    client, store, svc, repo = app_env
    headers = login(client)
    sha = _base_sha(repo)

    patch_file = repo / 'traversal-fix.txt'
    patch_file.write_text('x\n')
    subprocess.run(['git', 'add', 'traversal-fix.txt'], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'fix'], cwd=repo,
                   check=True, capture_output=True,
                   env={**dict(__import__('os').environ),
                        'GIT_AUTHOR_NAME': 'T', 'GIT_COMMITTER_NAME': 'T',
                        'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_EMAIL': 't@t'})
    commit_sha = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()

    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='ready_for_review',
        base_sha=sha,
        artifacts={
            'commit': commit_sha,
            'base_sha': sha,
            'worktree': str(repo),
            'checks': [],
        })

    res = client.get(
        f'/api/v2/maintenance/tasks/{tid}/artifacts/../../etc/passwd',
        headers=headers)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# refusal edges
# ---------------------------------------------------------------------------

def test_unauthenticated_refused(app_env):
    client, store, svc, repo = app_env
    # No login
    res = client.get('/api/v2/maintenance/tasks?project_id=any')
    assert res.status_code == 401

    res = client.post('/api/v2/maintenance/tasks', json={})
    assert res.status_code in (401, 403)


def test_other_users_project_refused(app_env):
    """A member without project assignment cannot read another project's tasks."""
    client, store, svc, repo = app_env
    admin_headers = login(client)
    tid, p, _, _ = _controlled_task(client, store, repo, admin_headers)

    # Create a member with no project assignments
    auth = client.app.state.auth
    auth.create_user('outsider', 'outsider-long-password', role='member')
    token, csrf, _ = auth.login('outsider', 'outsider-long-password')
    from factory.control.app import COOKIE
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    member_headers = {'Origin': 'http://testserver', 'X-CSRF-Token': csrf}

    res = client.get(f'/api/v2/maintenance/tasks/{tid}', headers=member_headers)
    assert res.status_code == 403


def test_unknown_task_404(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    res = client.get('/api/v2/maintenance/tasks/nonexistent-task-id', headers=headers)
    assert res.status_code == 404


def test_malformed_request_422(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    # Missing required fields
    res = client.post('/api/v2/maintenance/tasks',
                      json={'issue': 'not-a-dict'},
                      headers=headers)
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# the shape the web form actually sends
# ---------------------------------------------------------------------------

def test_the_agreement_names_the_installed_method_pack(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    res = client.get('/api/v2/maintenance/agreement', headers=headers)
    assert res.status_code == 200, res.text
    info = res.json()
    # Read from the pack, not typed by a form: the version a receipt records is
    # the version that is installed.
    from factory.control.agent_packs import catalog
    pack = next(p for p in catalog() if p['id'] == 'issue-maintenance')
    assert info['skill_version'] == f"issue-maintenance@{pack['version']}"
    assert info['pack']['validation_status'] == pack['validation_status']
    assert info['revision']


def test_create_accepts_exactly_the_body_the_create_form_sends(app_env):
    """The baseline travels flat, beside an agreement the server handed out.

    A nested ``baseline`` object is what the form sent before this was pinned,
    and the port rejected it as "missing repository, base_sha, agreement" --
    a break nothing but a real request could show.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    agreement = client.get('/api/v2/maintenance/agreement', headers=headers).json()
    key = f'form-{uuid.uuid4().hex[:12]}'
    body = {
        'idempotency_key': key,
        'issue': {'source': 'manual', 'external_id': key, 'version': '1',
                  'title': '跨月导出丢最后一行', 'body': '月度导出永远缺最后一个月。'},
        'project_id': p['id'],
        'repository': p['repository'],
        'base_sha': _base_sha(repo),
        'base_branch_label': p['base_branch'],
        'expected_behaviour': '闭区间：起止月份都包含在结果里',
        'delivery_goal': '导出可下载补丁并给出回执',
        'agreement': {'revision': agreement['revision'],
                      'skill_version': agreement['skill_version']},
    }
    res = client.post('/api/v2/maintenance/tasks', json=body, headers=headers)
    assert res.status_code == 201, res.text
    view = res.json()
    assert view['baseline']['repository'] == p['repository']
    assert view['agreement']['skill_version'] == agreement['skill_version']


def test_a_gate_with_no_plan_is_not_offered_as_resumable(app_env):
    """Waiting is not the same as resumable, and the view must not blur them."""
    client, store, svc, repo = app_env
    headers = login(client)
    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='needs_human')

    view = client.get(f'/api/v2/maintenance/tasks/{tid}', headers=headers).json()
    assert view['status'] == 'waiting'
    assert view['resumable'] is False, '没有计划的人工节点上，继续执行一定会被拒绝'

    store.update(rid, {'plan': {'tasks': [{'id': 'fix', 'title': '修'}]}})
    view = client.get(f'/api/v2/maintenance/tasks/{tid}', headers=headers).json()
    assert view['resumable'] is True


def test_a_plan_waiting_for_approval_says_so_instead_of_unknown(app_env):
    """A gate with the plan in hand must not render as "no citable reason"."""
    client, store, svc, repo = app_env
    headers = login(client)
    tid, p, rid, _ = _controlled_task(
        client, store, repo, headers, execution_status='awaiting_approval')
    store.update(rid, {'plan': {'title': '修一处区间',
                                'tasks': [{'id': 'fix', 'title': '修一处区间',
                                           'paths': ['report.py'],
                                           'checks': ['report']}]}})

    view = client.get(f'/api/v2/maintenance/tasks/{tid}', headers=headers).json()
    assert view['status'] == 'waiting'
    assert view['blocking_reason']['kind'] == 'approval.required'
    plan = view['pending_plan']
    # What the approver is agreeing to, not just that something is pending.
    assert plan['tasks'][0]['paths'] == ['report.py']
    assert plan['tasks'][0]['checks'] == ['report']
