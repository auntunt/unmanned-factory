import hashlib
import hmac
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory.control.app import create_app
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store


class FakeSDK:
    def available(self):
        return [{'id': 'codex', 'installed': True, 'detail': 'test double'}]

    def run(self, request, emit, cancel=None):
        if request.read_only:
            plan = {'title': 'Update greeting', 'summary': 'Update greeting with an independent check.',
                    'questions': [], 'tasks': [{'id': 'greeting', 'title': 'Update greeting',
                    'prompt': 'Update greeting.txt to hello world', 'acceptance': ['greeting is hello world'],
                    'paths': ['greeting.txt'], 'checks': ['greeting'], 'depends_on': [],
                    'complexity': 'small', 'risk': 'low'}]}
            return ProviderResult(json.dumps(plan), cost_usd=0.01)
        (Path(request.workspace) / 'greeting.txt').write_text('hello world')
        emit('assistant.message', {'text': 'Updated the greeting.'})
        return ProviderResult('Updated the greeting.', cost_usd=0.01)


def wait_state(store, rid, states):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = store.get(rid)
        if run['status'] in states:
            return run
        time.sleep(0.02)
    raise AssertionError(store.get(rid))


@pytest.fixture
def app_env(tmp_path):
    repo = tmp_path / 'repos' / 'sample'
    repo.mkdir(parents=True)
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'Test'],
                 ['config', 'user.email', 'test@example.com']):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    (repo / 'greeting.txt').write_text('hello')
    subprocess.run(['git', 'add', 'greeting.txt'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True)
    data = tmp_path / 'data'
    store = Store(data / 'control.db')
    profiles = {k: {'provider': 'codex', 'model': 'test'} for k in ('planner', 'cheap', 'standard', 'strong')}
    svc = Service(store, runner=FakeSDK(), profiles=profiles)
    app = create_app(data_dir=data, workspace_root=tmp_path / 'repos',
                     public_origin='http://testserver', service=svc, webhook_secret='test-webhook-secret')
    app.state.auth.create_user('owner', 'a-long-test-password')
    with TestClient(app) as client:
        yield client, store, svc, repo


def login(client):
    response = client.post('/api/auth/login', json={'username': 'owner', 'password': 'a-long-test-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200, response.text
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}


def project(client, repo, headers):
    response = client.post('/api/v2/projects', json={'name': 'Sample', 'repository': 'owner/sample',
        'workspace': str(repo), 'checks': {'greeting': [sys.executable, '-c',
            "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello world'"]}}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_login_guards_all_api_including_legacy(app_env):
    client, store, svc, repo = app_env
    for path in ('/api/v2/runs', '/api/tasks', '/api/events', '/api/auth/me'):
        assert client.get(path).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'owner', 'password': 'a-long-test-password'}).status_code == 403
    headers = login(client)
    assert client.get('/api/auth/me').json()['user']['username'] == 'owner'
    assert client.post('/api/v2/projects', json={}, headers={'Origin': 'http://testserver'}).status_code == 403
    assert client.post('/api/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/v2/runs').status_code == 401


def test_requirement_to_verified_git_delivery_and_conversation(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    response = client.post('/api/v2/runs', json={'project_id': p['id'], 'request': 'Update the greeting'}, headers=headers)
    assert response.status_code == 201, response.text
    rid = response.json()['id']
    run = wait_state(store, rid, {'awaiting_approval', 'needs_human'})
    assert run['status'] == 'awaiting_approval', run
    assert client.post(f'/api/v2/runs/{rid}/approve', json={'revision': 99}, headers=headers).status_code == 409
    assert client.post(f'/api/v2/runs/{rid}/approve', json={'revision': run['revision']}, headers=headers).status_code == 200
    run = wait_state(store, rid, {'ready_for_review', 'needs_human'})
    assert run['status'] == 'ready_for_review', (run, store.events(rid))
    artifacts = run['artifacts']
    assert (Path(artifacts['worktree']) / 'greeting.txt').read_text() == 'hello world'
    assert (repo / 'greeting.txt').read_text() == 'hello'
    assert subprocess.check_output(['git', 'show', artifacts['commit'] + ':greeting.txt'], cwd=repo, text=True) == 'hello world'
    assert all(c['exit'] == 0 for c in artifacts['checks'])
    messages = client.get(f'/api/v2/runs/{rid}/conversation').json()['messages']
    assert messages[0]['role'] == 'user'
    assert sum(m['content'] == 'Updated the greeting.' for m in messages) == 1
    export = client.get(f'/api/v2/runs/{rid}/export')
    assert export.status_code == 200 and artifacts['commit'] in export.text
    events = client.get(f'/api/v2/runs/{rid}/events').json()
    assert events['events'][0]['id'] > 0
    assert client.get(f"/api/v2/runs/{rid}/events?after={events['cursor']}").json()['events'] == []


def test_clarification_invalidates_approval(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    from factory.control.autonomy import DEFAULT_POLICY
    svc.policies.update(p['id'], {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'test')
    rid = client.post('/api/v2/runs', json={'project_id': p['id'], 'request': 'Update greeting'}, headers=headers).json()['id']
    first = wait_state(store, rid, {'awaiting_approval'})
    assert client.post(f'/api/v2/runs/{rid}/clarify', json={'answer': 'Also keep the format'}, headers=headers).status_code == 200
    second = wait_state(store, rid, {'awaiting_approval'})
    assert second['revision'] == first['revision'] + 1
    assert client.post(f'/api/v2/runs/{rid}/approve', json={'revision': first['revision']}, headers=headers).status_code == 409


def test_webhook_signatures_allowlist_and_replay(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    project(client, repo, headers)
    payload = {'action': 'opened', 'repository': {'full_name': 'owner/sample'},
               'issue': {'number': 5, 'title': 'Update greeting', 'body': 'Expected hello world',
                         'labels': [], 'updated_at': '2026-09-07T00:00:00Z'}}
    def send(data, delivery='delivery-1', signature=True):
        raw = json.dumps(data).encode()
        sig = 'sha256=' + hmac.new(b'test-webhook-secret', raw, hashlib.sha256).hexdigest()
        return client.post('/api/v2/github/webhook', content=raw, headers={
            'X-GitHub-Event': 'issues', 'X-GitHub-Delivery': delivery,
            'X-Hub-Signature-256': sig if signature else 'sha256=fake'})
    assert send(payload, signature=False).status_code == 401
    first = send(payload).json()
    assert not first['duplicate']
    assert send(payload).json()['duplicate']
    assert send(payload, 'delivery-2').json()['run_id'] == first['run_id']
    payload['issue']['body'] = 'changed content under same delivery'
    assert send(payload).json()['duplicate']
    assert len(store.runs()) == 1
    run = wait_state(store, first['run_id'], {'awaiting_approval'})
    assert run['triage']['decision'] != 'auto_execute'
    payload['issue']['updated_at'] = '2026-09-07T00:01:00Z'
    revised = send(payload, 'delivery-revised').json()
    assert revised['run_id'] != first['run_id']
    revised_run = wait_state(store, revised['run_id'], {'awaiting_approval'})
    assert revised_run['source']['previous_run_id'] == first['run_id']
    assert revised_run['triage']['decision'] != 'auto_execute'
    assert store.get(first['run_id'])['status'] == 'cancelled'
    payload['repository']['full_name'] = 'other/repo'
    assert send(payload, 'delivery-3').json()['ignored']


def test_request_limit_and_workspace_boundary(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    assert client.post('/api/auth/login', content=b'x' * 1_048_577,
                       headers={'Origin': 'http://testserver'}).status_code == 413
    assert client.post('/api/v2/projects', json={'name': 'Bad', 'repository': 'a/b',
        'workspace': str(repo.parent.parent)}, headers=headers).status_code == 400
    p = project(client, repo, headers)
    assert client.post('/api/v2/projects', json={'name': 'Duplicate', 'repository': p['repository'],
        'workspace': str(repo)}, headers=headers).status_code == 409


def test_redaction_persistence_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORY_GITHUB_TOKEN', 'specific-long-secret')
    store = Store(tmp_path / 'audit.db')
    run, _ = store.create_run('p', 'Do the thing')
    store.append(run['id'], 'tool.result', {'password': 'cleartext', 'nested': {
        'message': 'specific-long-secret', 'thinking': 'private'}, 'tokens_in': 42})
    event = store.events(run['id'])[-1]
    assert event['payload']['password'] != 'cleartext'
    assert event['payload']['nested']['message'] != 'specific-long-secret'
    assert 'thinking' not in event['payload']['nested']
    assert event['payload']['tokens_in'] == 42
    store.append(run['id'], 'tool.result', {'stdout': 'x' * 100_001})
    assert '输出已截断' in store.events(run['id'])[-1]['payload']['stdout']
    reopened = Store(tmp_path / 'audit.db')
    reopened.recover()
    assert reopened.get(run['id'])['status'] == 'needs_human'
    assert reopened.events(run['id'])[-1]['type'] == 'run.recovered'
    with pytest.raises(Exception, match='append-only'):
        with reopened.connect() as db:
            db.execute('DELETE FROM events')


def test_paused_clarification_carries_failure_evidence_to_new_plan(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    from factory.control.autonomy import DEFAULT_POLICY
    svc.policies.update(p['id'], {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'test')
    rid = client.post('/api/v2/runs', json={'project_id': p['id'], 'request': 'Update greeting'}, headers=headers).json()['id']
    first = wait_state(store, rid, {'awaiting_approval'})
    error = 'out-of-scope changes: src/api.py, tests/__init__.py'
    store.update(rid, {'status': 'needs_human', 'tasks': [{'id': 'scaffold', 'status': 'failed', 'attempts': [{'error': error}]}]})
    received = []
    monkeypatch.setattr(svc, 'start_plan', lambda run_id: received.append(store.get(run_id)))
    answer = '保持原需求，检查文件所属任务后重新规划'
    assert client.post(f'/api/v2/runs/{rid}/clarify', json={'answer': answer}).status_code == 403
    response = client.post(f'/api/v2/runs/{rid}/clarify', json={'answer': answer}, headers=headers)
    assert response.status_code == 200
    assert len(received) == 1
    assert error in received[0]['history'][-2]
    assert received[0]['history'][-1] == answer
    assert received[0]['plan'] is None and received[0]['tasks'] == []
    assert client.post(f'/api/v2/runs/{rid}/approve', json={'revision': first['revision']}, headers=headers).status_code == 409
