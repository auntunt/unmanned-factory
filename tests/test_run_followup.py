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
    assert body['queued'] is True and body['message']
    messages = client.get(f'/api/v2/runs/{rid}/conversation', headers=headers).json()['messages']
    assert len(messages) == before + 1
    assert messages[-1]['role'] == 'user' and messages[-1]['content'] == '再加一个按客户名搜索'
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
