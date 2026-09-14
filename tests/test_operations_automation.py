import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from factory.control.store import scrub
from tests.test_control_app import app_env, login, project

WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/example-secret-identifier'


def setup(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    return client, store, service.operations_automation, p, headers


def new_run(store, pid, inspection=False):
    return store.create_run(pid, '检查启动', source={'type': 'inspection' if inspection else 'web', 'operation': 'startup'})[0]


def complete(store, run, status='inspection_failed', reason='connection refused', artifacts=None):
    return store.update(run['id'], {'status': status, 'error': reason, 'artifacts': artifacts or {}},
                        event=('inspection.failed' if status == 'inspection_failed' else 'run.failed', {'message': reason}))


def verified_artifacts():
    fact = {'status': 'pass', 'evidence': 'npm start', 'criterion_ids': ['startup']}
    return {'verification': {'verdict': 'pass', 'operation_results': {'startup_command': fact}},
            'operation_results': {'startup_command': fact},
            'acceptance_ledger': {'items': [{'id': 'startup', 'status': 'pass'}]}}


def test_config_default_secret_permissions_and_admin_api(app_env):
    client, store, automation, p, headers = setup(app_env)
    assert automation.config() == {'revision': 0, 'knowledge_enabled': False, 'webhook_configured': False}
    response = client.put('/api/v2/runtime/operations', headers=headers,
        json={'revision': 0, 'knowledge_enabled': True, 'webhook': WEBHOOK})
    assert response.status_code == 200 and WEBHOOK not in response.text
    assert stat.S_IMODE(automation.path.stat().st_mode) == 0o600
    assert automation.config(True)['webhook'] == WEBHOOK
    assert WEBHOOK not in scrub({'webhook': WEBHOOK, 'reason': 'failed ' + WEBHOOK}).__str__()
    assert client.put('/api/v2/runtime/operations', headers=headers,
        json={'revision': 0, 'knowledge_enabled': False}).status_code == 409
    assert client.put('/api/v2/runtime/operations', headers=headers,
        json={'revision': 1, 'knowledge_enabled': False, 'webhook': 'http://localhost/secret'}).status_code == 400
    automation.configure(1, False, '')
    assert not automation.config()['webhook_configured']
    client.app.state.auth.create_user('member-notify', 'very-long-password', role='member')
    response = client.post('/api/auth/login', headers={'Origin': 'http://testserver'},
                           json={'username': 'member-notify', 'password': 'very-long-password'})
    member = {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}
    assert client.get('/api/v2/runtime/operations').status_code == 403
    assert client.put('/api/v2/runtime/operations', headers=member,
                     json={'revision': 2, 'knowledge_enabled': True}).status_code == 403


def test_notification_concurrent_dedup_redaction_and_failure_isolation(app_env, monkeypatch, caplog):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, False, WEBHOOK)
    run = new_run(store, p['id'])
    sent = []
    monkeypatch.setattr(automation, 'send', lambda url, text: sent.append(text))
    reason = 'password=hidden-password ' + WEBHOOK + '错误' * 250
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: automation.notify(run, reason), range(8)))
    assert len(sent) == 1
    assert len(sent[0].splitlines()[1]) <= 200
    assert 'hidden-password' not in sent[0] and WEBHOOK not in sent[0]
    assert p['name'] in sent[0] and f"http://testserver/runs/{run['id']}" in sent[0]
    def unavailable(*args):
        raise RuntimeError(WEBHOOK)
    monkeypatch.setattr(automation, 'send', unavailable)
    complete(store, run, status='needs_human', reason='budget exhausted')
    automation.tick()
    automation.tick()
    assert store.get(run['id'])['status'] == 'needs_human'
    assert WEBHOOK not in caplog.text
    assert 'Operations notification failed' in caplog.text


def test_history_streak_unverified_and_supersession(app_env, monkeypatch):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, False, WEBHOOK)
    sent = []
    monkeypatch.setattr(automation, 'send', lambda url, text: sent.append(text))
    for i in range(3):
        run = new_run(store, p['id'], inspection=True)
        complete(store, run, artifacts={'verification': {'verdict': 'unverified', 'reason': 'Chrome unavailable'}})
        automation.tick()
    result = automation.history(p['id'])
    assert result['consecutive_failures'] == 3
    assert result['history'][0]['verdict'] == 'unverified'
    assert result['history'][0]['failure_category'] == 'environment'
    assert result['history'][0]['duration_s'] >= 0
    assert '持续故障' in sent[-1]
    store.update(run['id'], {'inspection_superseded_by': 'replacement'})
    automation.tick()
    assert len(automation.history(p['id'])['history']) == 3
    passed = new_run(store, p['id'], inspection=True)
    complete(store, passed, 'inspection_completed')
    automation.tick()
    assert automation.history(p['id'])['consecutive_failures'] == 0


def test_github_failure_and_disabled_notification(app_env, monkeypatch):
    client, store, automation, p, headers = setup(app_env)
    sent = []
    monkeypatch.setattr(automation, 'send', lambda *args: sent.append(args))
    run = new_run(store, p['id'])
    complete(store, run, status='needs_human')
    automation.tick()
    assert not sent
    automation.configure(0, False, WEBHOOK)
    store.append(run['id'], 'github.publish_failed', {'message': 'GitHub denied push'})
    automation.tick()
    assert len(sent) == 1 and 'GitHub denied push' in sent[0][1]


def test_fact_feedback_default_off_verified_only_replace_delete_and_compile(app_env, monkeypatch):
    client, store, automation, p, headers = setup(app_env)
    run = new_run(store, p['id'])
    complete(store, run, 'ready_for_review', artifacts=verified_artifacts())
    automation.tick()
    assert not automation.facts(p['id'])
    automation.configure(0, True)
    run = new_run(store, p['id'])
    complete(store, run, 'ready_for_review', artifacts=verified_artifacts())
    automation.tick()
    first = automation.facts(p['id'])[0]
    assert first['provenance']['run_id'] == run['id']
    assert 'npm start' in automation.context(p['id'])
    newer = new_run(store, p['id'])
    artifacts = verified_artifacts()
    artifacts['operation_results']['startup_command']['evidence'] = 'npm run serve'
    complete(store, newer, 'published', artifacts=artifacts)
    automation.tick()
    fact = automation.facts(p['id'])[0]
    assert fact['content'] == 'npm run serve' and fact['revision'] == first['revision'] + 1
    monkeypatch.setattr(client.app.state.service, 'start_plan', lambda rid: None)
    body = {'project_id': p['id'], 'request': '排错', 'operation': 'startup', 'idempotency_key': 'fact-request-key'}
    created = client.post('/api/v2/runs', json=body, headers=headers).json()
    assert 'npm run serve' in created['request']
    payload = {key: fact[key] for key in ('kind', 'title', 'content', 'paths', 'commit_sha')}
    payload.update(status='retired', expected_revision=fact['revision'])
    assert client.put(f"/api/v2/projects/{p['id']}/knowledge/{fact['key']}", headers=headers, json=payload).status_code == 200
    automation._learn(store.get(newer['id']))
    assert not automation.facts(p['id'])
    assert client.post('/api/v2/runs', json=body, headers=headers).json()['id'] == created['id']
    automation.configure(1, False)
    assert automation.context(p['id']) == ''


@pytest.mark.parametrize('failure', ['unverified', 'unbound', 'unsupported'])
def test_unproven_claims_never_become_facts(app_env, failure):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, True)
    artifacts = verified_artifacts()
    if failure == 'unverified':
        artifacts['acceptance_ledger']['items'][0]['status'] = 'unverified'
    elif failure == 'unbound':
        artifacts['operation_results'] = {}
    else:
        artifacts['verification']['operation_results']['startup_command']['criterion_ids'] = ['invented']
    run = new_run(store, p['id'])
    complete(store, run, 'published', artifacts=artifacts)
    automation.tick()
    assert not automation.facts(p['id'])


def test_webhook_wire_format_and_rejected_response(monkeypatch):
    from factory.control.operations_automation import OperationsAutomation
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, maximum): return json.dumps({'code': self.code}).encode()
        code = 0
    class Opener:
        def open(self, request, timeout):
            captured.append((request, timeout))
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda handler: Opener())
    OperationsAutomation.send(WEBHOOK, '测试消息')
    request, timeout = captured[0]
    assert json.loads(request.data) == {'msg_type': 'text', 'content': {'text': '测试消息'}}
    assert timeout == 5
    Response.code = 19001
    with pytest.raises(ValueError, match='rejected'):
        OperationsAutomation.send(WEBHOOK, '测试消息')


def test_outbox_and_dedup_survive_restart(app_env, monkeypatch):
    from factory.control.operations_automation import OperationsAutomation
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, False, WEBHOOK)
    run = new_run(store, p['id'])
    complete(store, run, 'needs_human', 'budget exhausted')
    restored = OperationsAutomation(store)
    sent = []
    monkeypatch.setattr(restored, 'send', lambda *args: sent.append(args))
    restored.tick()
    complete(store, run, 'needs_human', 'budget exhausted')
    restored.tick()
    assert len(sent) == 1
    complete(store, run, 'needs_human', 'another error')
    restored.tick()
    assert len(sent) == 2


def test_inspection_request_includes_facts_and_history_is_limited(app_env):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, True)
    run = new_run(store, p['id'])
    complete(store, run, 'published', artifacts=verified_artifacts())
    automation.tick()
    inspections = client.app.state.service.inspections
    config = inspections.configure(p['id'], enabled=True, interval_s=300, revision=0, actor_id=1)
    rid = inspections.tick(config['next_at'])[0]
    assert 'npm start' in store.get(rid)['request']
    for index in range(22):
        run = new_run(store, p['id'], inspection=True)
        complete(store, run, 'inspection_failed')
    automation.tick()
    assert len(automation.history(p['id'])['history']) == 20
    assert automation.history(p['id'])['consecutive_failures'] == 22


def test_manual_edit_does_not_forge_verified_fact(app_env):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, True)
    run = new_run(store, p['id'])
    complete(store, run, 'published', artifacts=verified_artifacts())
    automation.tick()
    fact = automation.facts(p['id'])[0]
    data = {key: fact[key] for key in ('kind', 'status', 'title', 'content', 'paths', 'commit_sha')}
    data['content'] = 'an unverified replacement command'
    automation.memory.put_entry(p['id'], data, 'owner', key=fact['key'], expected_revision=fact['revision'])
    assert automation.facts(p['id']) == []


def test_operational_facts_continue_past_human_entry_revision_cap(app_env):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, True)
    for _ in range(101):
        run = new_run(store, p['id'])
        complete(store, run, 'published', artifacts=verified_artifacts())
        automation.tick()
    fact = automation.facts(p['id'])[0]
    assert fact['revision'] == 101
    data = {key: fact[key] for key in ('kind', 'title', 'content', 'paths', 'commit_sha')}
    data['status'] = 'retired'
    automation.memory.put_entry(p['id'], data, 'owner', key=fact['key'], expected_revision=101)
    assert not automation.facts(p['id'])


def test_cancelled_inspection_is_unverified_history_without_alarm(app_env, monkeypatch):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, False, WEBHOOK)
    sent = []
    monkeypatch.setattr(automation, 'send', lambda *args: sent.append(args))
    run = new_run(store, p['id'], inspection=True)
    complete(store, run, 'cancelled', '巡检已取消')
    automation.tick()
    assert automation.history(p['id'])['history'][0]['verdict'] == 'unverified'
    assert not sent


def test_github_repository_setup_failure_also_notifies(app_env, monkeypatch):
    client, store, automation, p, headers = setup(app_env)
    automation.configure(0, False, WEBHOOK)
    run = new_run(store, p['id'])
    complete(store, run, 'ready_for_review')
    def failed(*args, **kwargs):
        raise RuntimeError('GitHub connection unavailable')
    monkeypatch.setattr('factory.control.github_publication.GitHubPublication.publish', failed)
    sent = []
    monkeypatch.setattr(automation, 'send', lambda *args: sent.append(args))
    with pytest.raises(RuntimeError):
        client.app.state.service.publish_github(run['id'])
    automation.tick()
    assert len(sent) == 1
