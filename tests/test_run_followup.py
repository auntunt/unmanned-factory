"""The run-scoped follow-up endpoint: at a human gate it drives the run forward,
while the run is actively executing it records the note in the conversation
(never silently dropped, never faked as applied), and it refuses a finished run."""
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
    assert body['queued'] is False and '尚未加入' in body['message']
    messages = client.get(f'/api/v2/runs/{rid}/conversation', headers=headers).json()['messages']
    assert len(messages) == before + 1
    assert messages[-1]['role'] == 'user' and messages[-1]['content'] == '再加一个按客户名搜索'
    assert messages[-1]['followup'] is True and messages[-1]['applied'] is False
    # It is persisted as an immutable event, so it survives and is not lost.
    assert any(e['type'] == 'user.message' and e['payload'].get('followup') for e in store.events(rid))


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
