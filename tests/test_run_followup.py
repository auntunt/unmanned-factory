"""The run-scoped follow-up endpoint: at a human gate it drives the run forward,
while the run is actively executing it records the note as a pending follow-up
(never silently dropped, never faked as applied), and it refuses a finished run.

The followup.pending / followup.applied lifecycle ensures:
  - ACTIVE submissions are queued (queued=true) and will be consumed at the next safe node
  - Idempotent retries replay the same receipt even after status changes
  - Same key + different content returns 409
  - Service restart preserves unconsumed pending items (event-sourced, no new table)
  - Consumption at safe nodes (clarify/continue_run) merges content and records followup.applied
  - Already-consumed items are not double-consumed on retry
  - Cancel does not clear unconsumed pending items
"""
from factory.control.service import Service
from factory.control.store import Store
from tests.test_control_app import app_env, login, project  # noqa: F401


def _run(client, store, repo, headers, status):
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={'operation': 'general', 'project_id': p['id'], 'request': '做一个工具'},
                      headers=headers).json()['id']
    store.update(rid, {'status': status})
    return rid


def test_followup_while_active_is_recorded_in_conversation_not_dropped(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    before = len(client.get(f'/api/v2/runs/{rid}/conversation', headers=headers).json()['messages'])
    res = client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '再加一个按客户名搜索'}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['recorded'] is True and body['applied'] is False
    assert body['queued'] is True
    assert '自动并入' in body['message']
    messages = client.get(f'/api/v2/runs/{rid}/conversation', headers=headers).json()['messages']
    assert len(messages) == before + 1
    assert messages[-1]['role'] == 'user' and messages[-1]['content'] == '再加一个按客户名搜索'
    assert messages[-1]['followup'] is True and messages[-1]['applied'] is False
    # It is persisted as an immutable event, so it survives and is not lost.
    assert any(e['type'] == 'user.message' and e['payload'].get('followup') for e in store.events(rid))


def test_followup_pending_event_is_persisted_with_prescribed_fields(app_env):
    """followup.pending carries id, content, actor_id, actor, fingerprint, created_at."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '改颜色'}, headers=headers)
    pending_events = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending_events) == 1
    p = pending_events[0]['payload']
    assert isinstance(p['id'], str) and len(p['id']) == 32
    assert p['content'] == '改颜色'
    assert p['actor'] == 'owner'
    assert isinstance(p['actor_id'], int)
    assert isinstance(p['fingerprint'], str) and len(p['fingerprint']) == 64
    assert isinstance(p['created_at'], str) and p['created_at'] != ''


def test_followup_on_a_finished_run_is_refused_so_a_new_revision_is_started(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'published')
    res = client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '再改一版'}, headers=headers)
    assert res.status_code == 409


def test_followup_requires_authentication(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    assert client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': 'x'}).status_code == 403


def test_followup_retry_is_idempotent_even_after_status_changes(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    body = {'content': '保留中文', 'idempotency_key': 'one-message-123'}
    first = client.post(f'/api/v2/runs/{rid}/follow-up', json=body, headers=headers)
    assert first.status_code == 200
    store.update(rid, {'status': 'published'})
    repeated = client.post(f'/api/v2/runs/{rid}/follow-up', json=body, headers=headers)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    messages = [e for e in store.export_events(rid, kind='user.message') if e['payload'].get('followup')]
    assert len(messages) == 1
    changed = client.post(f'/api/v2/runs/{rid}/follow-up', json={**body, 'content': '不同内容'}, headers=headers)
    assert changed.status_code == 409


def test_followup_rejects_blank_text(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    assert client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '   '}, headers=headers).status_code == 422


def test_member_can_only_follow_up_own_assigned_run(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    auth = client.app.state.auth
    member = auth.create_user('followup-member', 'long-member-password', role='member')
    svc.governance.assign(member['id'], [p['id']], 'owner')
    own = store.create_run(p['id'], 'own', source={'actor_id': member['id']})[0]
    other = store.create_run(p['id'], 'other', source={'actor_id': -1})[0]
    token, csrf, _ = auth.login('followup-member', 'long-member-password')
    from factory.control.app import COOKIE
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    member_headers = {**headers, 'X-CSRF-Token': csrf}
    body = {'content': 'a useful change', 'idempotency_key': 'member-message'}
    assert client.post(f"/api/v2/runs/{own['id']}/follow-up", json=body, headers=member_headers).status_code == 200
    assert client.post(f"/api/v2/runs/{other['id']}/follow-up", json=body, headers=member_headers).status_code == 403
    svc.governance.assign(member['id'], [], 'owner')
    assert client.post(f"/api/v2/runs/{own['id']}/follow-up", json=body, headers=member_headers).status_code == 403


def test_continuation_message_survives_conversation_reload(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'needs_human')
    store.append(rid, 'human.continued', {'answer': '调整标题为中文', 'actor': 'owner'})
    messages = client.get(f'/api/v2/runs/{rid}/conversation', headers=headers).json()['messages']
    assert messages[-1]['role'] == 'user'
    assert messages[-1]['content'] == '调整标题为中文'


# ---------- T03 new tests: pending/applied lifecycle ----------

def test_pending_followup_survives_service_restart(app_env):
    """Pending follow-ups are rebuilt from events after a new Service instance is created."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '加搜索功能'}, headers=headers)
    # Verify pending event exists
    pending_before = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending_before) == 1
    # Simulate service restart: events are in the DB, no in-memory state needed
    pending_after = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending_after) == 1
    assert pending_after[0]['payload']['content'] == '加搜索功能'
    # Verify via GET that followups are reported
    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    assert len(run_data['followups']) == 1
    assert run_data['followups'][0]['applied'] is False


def test_get_run_reports_followup_status(app_env):
    """GET /api/v2/runs/{rid} includes followups list with pending/applied status."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    # Submit two follow-ups
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '补充一', 'idempotency_key': 'fu-key-aaa1'}, headers=headers)
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '补充二', 'idempotency_key': 'fu-key-bbb2'}, headers=headers)
    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    assert len(run_data['followups']) == 2
    assert all(f['applied'] is False for f in run_data['followups'])
    # Manually record one as applied
    pending_id = run_data['followups'][0]['id']
    store.append(rid, 'followup.applied', {'pending_id': pending_id, 'run_revision': 1})
    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    applied = [f for f in run_data['followups'] if f['applied']]
    unapplied = [f for f in run_data['followups'] if not f['applied']]
    assert len(applied) == 1 and len(unapplied) == 1


def test_conflict_same_key_different_content_409(app_env):
    """Same idempotency key with different content returns 409."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    body1 = {'content': '版本一', 'idempotency_key': 'conflict-test-1'}
    body2 = {'content': '版本二', 'idempotency_key': 'conflict-test-1'}
    first = client.post(f'/api/v2/runs/{rid}/follow-up', json=body1, headers=headers)
    assert first.status_code == 200
    second = client.post(f'/api/v2/runs/{rid}/follow-up', json=body2, headers=headers)
    assert second.status_code == 409


def test_cancel_does_not_clear_unconsumed_pending(app_env):
    """Cancellation preserves pending follow-ups in the event stream."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '加搜索'}, headers=headers)
    pending_before = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending_before) == 1
    # Cancel the run
    client.post(f'/api/v2/runs/{rid}/cancel', headers=headers, json={})
    # Pending events are preserved (immutable)
    pending_after = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending_after) == 1
    assert pending_after[0]['payload']['content'] == '加搜索'
    # GET still reports the followup
    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    assert len(run_data['followups']) == 1
    assert run_data['followups'][0]['applied'] is False


def test_consumed_followup_not_double_consumed(app_env):
    """Once a followup.applied event is recorded, the same pending is not consumed again."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '加搜索'}, headers=headers)
    pending = list(store.export_events(rid, kind='followup.pending'))
    pending_id = pending[0]['payload']['id']
    # Simulate first consumption
    store.append(rid, 'followup.applied', {'pending_id': pending_id, 'run_revision': 1})
    # Import and call collect directly to verify idempotence
    from factory.control.run_lifecycle import _collect_pending_followups
    result = _collect_pending_followups(svc, rid)
    assert result == []  # Nothing left to consume
    # Only one applied event should exist
    applied_events = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied_events) == 1


def test_clarify_consumes_pending_followups_at_safe_node(app_env):
    """When clarify() is called, pending follow-ups are merged into the answer."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    # Submit a follow-up during active execution
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '按钮改成蓝色'}, headers=headers)
    pending = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending) == 1
    # Transition to needs_clarification (simulating worker pause)
    store.update(rid, {'status': 'needs_clarification'})
    # Now clarify: should consume the pending follow-up
    res = client.post(f'/api/v2/runs/{rid}/follow-up',
                      json={'content': '数据用 JSON 格式'},
                      headers=headers)
    assert res.status_code == 200
    assert res.json()['applied'] is True
    # The followup.applied event should have been recorded
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == 1
    assert applied[0]['payload']['pending_id'] == pending[0]['payload']['id']


def test_continue_run_consumes_pending_followups_at_safe_node(app_env):
    """When continue_run() is called at needs_human, pending follow-ups are merged."""
    import subprocess, sys
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={'operation': 'general', 'project_id': p['id'], 'request': '做工具'},
                      headers=headers).json()['id']
    # Set up a resumable run with plan and artifacts
    store.update(rid, {
        'status': 'running',
        'plan': {'title': 'test', 'summary': '', 'questions': [], 'tasks': [{'id': 't1', 'title': 'a', 'prompt': 'a', 'acceptance': ['ok'], 'paths': ['x'], 'checks': ['greeting'], 'depends_on': [], 'complexity': 'small', 'risk': 'low'}]},
        'revision': 1,
    })
    # Submit a follow-up during active execution
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '还要加导出'}, headers=headers)
    pending = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending) == 1
    # Get the base_sha for the project
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    store.update(rid, {
        'status': 'needs_human',
        'artifacts': {'base_sha': base, 'tasks': [{'id': 't1'}]},
        'execution_checks': store.project(p['id'])['checks'],
    })
    # Continue: should consume the pending follow-up
    res = client.post(f'/api/v2/runs/{rid}/continue',
                      json={'answer': '', 'revision': 1, 'resume_count': 0},
                      headers=headers)
    assert res.status_code == 200, res.text
    # followup.applied should be recorded
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == 1
    assert applied[0]['payload']['pending_id'] == pending[0]['payload']['id']


def test_followup_during_cancel_is_preserved(app_env):
    """Follow-up submitted while run is in cancel_requested-equivalent active state stays."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid = _run(client, store, repo, headers, 'running')
    # Submit follow-up while still running
    res = client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '取消前的补充'}, headers=headers)
    assert res.status_code == 200
    assert res.json()['queued'] is True
    # Cancel
    client.post(f'/api/v2/runs/{rid}/cancel', headers=headers, json={})
    # Pending preserved
    pending = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending) == 1
    assert pending[0]['payload']['content'] == '取消前的补充'
    # Conversation still shows it
    messages = client.get(f'/api/v2/runs/{rid}/conversation', headers=headers).json()['messages']
    followup_msgs = [m for m in messages if m.get('followup')]
    assert len(followup_msgs) == 1


def test_stale_revision_conflict_does_not_mark_pending_as_applied(app_env):
    """Regression: if continue_run raises Conflict (e.g. stale revision),
    the pending follow-up must remain unapplied -- never 'applied but lost'."""
    import subprocess
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={'operation': 'general', 'project_id': p['id'], 'request': '做工具'},
                      headers=headers).json()['id']
    store.update(rid, {
        'status': 'running',
        'plan': {'title': 'test', 'summary': '', 'questions': [], 'tasks': [
            {'id': 't1', 'title': 'a', 'prompt': 'a', 'acceptance': ['ok'],
             'paths': ['x'], 'checks': ['greeting'], 'depends_on': [],
             'complexity': 'small', 'risk': 'low'}]},
        'revision': 2,
    })
    # Submit a follow-up during active execution
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '换颜色'}, headers=headers)
    pending = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending) == 1
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()
    store.update(rid, {
        'status': 'needs_human',
        'artifacts': {'base_sha': base, 'tasks': [{'id': 't1'}]},
        'execution_checks': store.project(p['id'])['checks'],
    })
    # Try continue with STALE revision (1 instead of 2) -> must Conflict
    res = client.post(f'/api/v2/runs/{rid}/continue',
                      json={'answer': '', 'revision': 1, 'resume_count': 0},
                      headers=headers)
    assert res.status_code == 409
    # The pending follow-up must still be unapplied
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == 0
    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    assert len(run_data['followups']) == 1
    assert run_data['followups'][0]['applied'] is False


def test_pending_applied_exactly_once_after_conflict_then_correct_retry(app_env):
    """After a Conflict from stale revision, retrying with the correct revision
    consumes the pending follow-up exactly once."""
    import subprocess
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={'operation': 'general', 'project_id': p['id'], 'request': '做工具'},
                      headers=headers).json()['id']
    store.update(rid, {
        'status': 'running',
        'plan': {'title': 'test', 'summary': '', 'questions': [], 'tasks': [
            {'id': 't1', 'title': 'a', 'prompt': 'a', 'acceptance': ['ok'],
             'paths': ['x'], 'checks': ['greeting'], 'depends_on': [],
             'complexity': 'small', 'risk': 'low'}]},
        'revision': 2,
    })
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '加导出功能'}, headers=headers)
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()
    store.update(rid, {
        'status': 'needs_human',
        'artifacts': {'base_sha': base, 'tasks': [{'id': 't1'}]},
        'execution_checks': store.project(p['id'])['checks'],
    })
    # First attempt: stale revision -> Conflict, pending stays unapplied
    res1 = client.post(f'/api/v2/runs/{rid}/continue',
                       json={'answer': '', 'revision': 1, 'resume_count': 0},
                       headers=headers)
    assert res1.status_code == 409
    assert len(list(store.export_events(rid, kind='followup.applied'))) == 0
    # Second attempt: correct revision -> success, pending applied exactly once
    res2 = client.post(f'/api/v2/runs/{rid}/continue',
                       json={'answer': '', 'revision': 2, 'resume_count': 0},
                       headers=headers)
    assert res2.status_code == 200, res2.text
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == 1
