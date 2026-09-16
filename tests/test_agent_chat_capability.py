"""Gap #1: the no-project daily-chat path freezes the role/instructions/skill
version to the conversation and mounts only that role's granted materials
(bounded, read-only), with isolation, and without creating a project or run."""
import io
import time
import zipfile

from factory.control.providers import ProviderResult
from factory.control.agents import AgentStore
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401


def _skill(body='报价规则：单价乘以数量，再按等级打折。', ref='参考：VIP 客户 9 折。'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('SKILL.md', body)
        z.writestr('refs/rule.md', ref)
    return buf.getvalue()


def _agent(client, headers, service, name, skill=None):
    a = client.post('/api/v4/agents', json={'name': name, 'purpose': name}, headers=headers).json()
    if skill is not None:
        up = client.post(f"/api/v4/agents/{a['id']}/skills", files={'file': ('s.zip', skill, 'application/zip')}, headers=headers)
        assert up.status_code == 201, up.text
        assets = client.get(f"/api/v4/agents/{a['id']}/skills", headers=headers).json()['skills']
        draft = service.agents.save_draft(a['id'], {'skill_ids': [assets[0]['id']]}, 0)
        service.agents.apply(a['id'], draft['revision'], 'tester')
    return a


def _capture(monkeypatch, service):
    reqs = []
    def fake_run(req, emit, cancel):
        reqs.append(req)
        return ProviderResult(text='已根据已挂载资料作答')
    monkeypatch.setattr(service.runner, 'run', fake_run)
    return reqs


def _send(client, headers, cid, content):
    return client.post(f'/api/v4/conversations/{cid}/messages', json={'content': content}, headers=headers)


def _wait_job(service, store, job_id, deadline=5):
    end = time.time() + deadline
    while time.time() < end and service.maintenance_status(job_id)['status'] in ('pending', 'running', 'cancel_requested'):
        time.sleep(.02)
    return service.maintenance_status(job_id)['status']


def _do_convo(client, headers, aid):
    r = client.post(f'/api/v4/agents/{aid}/conversations', json={'mode': 'do'}, headers=headers)
    assert r.status_code == 201
    return r.json()['id']


def test_chat_freezes_version_and_mounts_only_this_role_materials(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    quote = _agent(client, headers, service, '报价助手', skill=_skill())
    other = _agent(client, headers, service, '会议总结助手', skill=_skill(body='会议纪要方法', ref='无关资料'))
    reqs = _capture(monkeypatch, service)

    cid = _do_convo(client, headers, quote['id'])
    first = _send(client, headers, cid, '这单报价多少？')
    assert first.status_code == 201 and first.json()['run'] is None  # daily chat: no run
    _wait_job(service, store, first.json()['job_id'])

    # A run was never created for a no-project chat.
    assert store.runs() == [] or all(r.get('source', {}).get('conversation_id') != cid for r in store.all_runs())
    # The mount carried THIS role's skill material, not the other role's.
    req = reqs[-1]
    assert req.reference_mount is not None
    texts = ' '.join(d.get('text', '') for d in req.reference_mount['documents'])
    assert '报价规则' in texts and '会议纪要方法' not in texts
    # The prompt used the frozen compiled instructions, not a live re-read.
    assert AgentStore(store).conversation_snapshot(cid)['version'] == 2

    # A different role's conversation only sees its own materials (isolation).
    cid2 = _do_convo(client, headers, other['id'])
    _send(client, headers, cid2, '帮我总结'); _wait_job(service, store, AgentStore(store).conversation(cid2)['messages'][-1]['job_id'])
    other_texts = ' '.join(d.get('text', '') for d in (reqs[-1].reference_mount or {'documents': []})['documents'])
    assert '会议纪要方法' in other_texts and '报价规则' not in other_texts


def test_role_update_does_not_change_an_existing_conversation(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    agents = AgentStore(store)
    reqs = _capture(monkeypatch, service)

    cid = _do_convo(client, headers, agent['id'])
    _wait_job(service, store, _send(client, headers, cid, '第一问').json()['job_id'])
    assert agents.conversation_snapshot(cid)['version'] == 1

    # Publish a new agent version (v2) after the conversation started.
    draft = agents.save_draft(agent['id'], {'instructions': '新版说明'}, 0)
    agents.apply(agent['id'], draft['revision'], 'tester')
    assert agents.version(agent['id'])['version'] == 2

    # The existing conversation keeps v1; a new conversation freezes v2.
    _wait_job(service, store, _send(client, headers, cid, '第二问').json()['job_id'])
    assert agents.conversation_snapshot(cid)['version'] == 1
    cid_new = _do_convo(client, headers, agent['id'])
    _wait_job(service, store, _send(client, headers, cid_new, '新会话').json()['job_id'])
    assert agents.conversation_snapshot(cid_new)['version'] == 2


def test_duplicate_send_while_answering_reuses_the_pending_job(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    import threading
    release = threading.Event()
    def slow(req, emit, cancel):
        release.wait(2)
        return ProviderResult(text='ok')
    monkeypatch.setattr(service.runner, 'run', slow)
    cid = _do_convo(client, headers, agent['id'])
    a = _send(client, headers, cid, '问一次')
    assert a.status_code == 201 and a.json()['status'] == 'pending'
    b = _send(client, headers, cid, '重复提交')
    # Same in-flight job is returned, not a second answer or a 409.
    assert b.status_code == 201 and b.json()['job_id'] == a.json()['job_id']
    release.set()
    _wait_job(service, store, a.json()['job_id'])


def test_chat_answer_can_be_cancelled_then_retried(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    import threading
    gate = threading.Event()
    from factory.control.providers import ProviderCancelled
    def slow(req, emit, cancel):
        gate.wait(2)
        if cancel.is_set(): raise ProviderCancelled('调用已取消')
        return ProviderResult(text='ok')
    monkeypatch.setattr(service.runner, 'run', slow)
    cid = _do_convo(client, headers, agent['id'])
    job = _send(client, headers, cid, '问一次').json()['job_id']
    cancelled = client.post(f'/api/v4/maintenance-jobs/{job}/cancel', headers=headers)
    assert cancelled.status_code == 200
    gate.set()
    assert _wait_job(service, store, job) == 'cancelled'
    # Retry by asking again in the same conversation; a fresh answer is produced.
    monkeypatch.setattr(service.runner, 'run', lambda req, emit, cancel: ProviderResult(text='重试后的回答'))
    again = _send(client, headers, cid, '再问一次')
    assert again.status_code == 201 and again.json()['status'] == 'pending'
    assert _wait_job(service, store, again.json()['job_id']) == 'completed'
    msgs = AgentStore(store).conversation(cid)['messages']
    assert any(m['role'] == 'assistant' and m.get('content') == '重试后的回答' for m in msgs)
