"""普通无项目 do 聊天的回答生命周期。

现场（f5b238c 服务器验收）：真实 job 已 completed、真实回答与导出都已保存，
但同一 job_id 下还留着一条 assistant `pending`「正在回答」。前端
AgentChatPage 以 messages.some(pending/running) 控制 typing 与轮询，于是
「正在回答」永远收不起来，刷新也一样。

这里锁住：一次真实发消息 → maintenance 完成 → 会话里不再有活动 pending，
且真实正文就在那条原消息上。注意本路径用的是 AgentStore / agent_conversations，
与 admin-config 是两套存储，不共用实现。
"""
import time

import pytest

from factory.control.providers import ProviderCancelled, ProviderResult
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401
from tests.test_agent_chat_capability import _agent, _do_convo, _send, _wait_job


def _active_pending(conv):
    return [m for m in conv['messages']
            if m.get('role') == 'assistant' and m.get('status') in ('pending', 'running')]


def test_completed_answer_leaves_no_active_pending(app_env, monkeypatch):
    client, store, service, repo = app_env
    h = login(client)
    aid = _agent(client, h, service, '报价助手')['id']
    cid = _do_convo(client, h, aid)

    monkeypatch.setattr(service.runner, 'run',
                        lambda req, emit, cancel: ProviderResult(text='报价单已生成：260.74 元'))

    r = _send(client, h, cid, '按会话资料报价')
    assert r.status_code == 201, r.text
    jid = r.json()['job_id']
    assert _wait_job(service, store, jid) == 'completed'

    conv = client.get(f'/api/v4/conversations/{cid}', headers=h).json()
    assert not _active_pending(conv), '完成后仍有活动 pending：' + repr(conv['messages'])

    answered = [m for m in conv['messages'] if m.get('job_id') == jid and m.get('role') == 'assistant']
    assert len(answered) == 1, '同一 job 出现多条 assistant 消息：' + repr(answered)
    assert answered[0]['status'] == 'completed', answered[0]
    assert answered[0]['content'] == '报价单已生成：260.74 元', answered[0]


def test_failed_answer_leaves_no_active_pending(app_env, monkeypatch):
    client, store, service, repo = app_env
    h = login(client)
    aid = _agent(client, h, service, '报价助手')['id']
    cid = _do_convo(client, h, aid)

    def boom(req, emit, cancel):
        raise RuntimeError('模型不可用')

    monkeypatch.setattr(service.runner, 'run', boom)

    r = _send(client, h, cid, '按会话资料报价')
    assert r.status_code == 201, r.text
    jid = r.json()['job_id']
    _wait_job(service, store, jid)

    conv = client.get(f'/api/v4/conversations/{cid}', headers=h).json()
    assert not _active_pending(conv), repr(conv['messages'])
    answered = [m for m in conv['messages'] if m.get('job_id') == jid and m.get('role') == 'assistant']
    assert len(answered) == 1, repr(answered)
    assert answered[0]['status'] == 'failed', answered[0]


def test_cancelled_answer_has_its_own_terminal_state(app_env, monkeypatch):
    client, store, service, repo = app_env
    h = login(client)
    aid = _agent(client, h, service, '报价助手')['id']
    cid = _do_convo(client, h, aid)

    def cancelled(req, emit, cancel):
        raise ProviderCancelled('用户取消')

    monkeypatch.setattr(service.runner, 'run', cancelled)

    r = _send(client, h, cid, '按会话资料报价')
    assert r.status_code == 201, r.text
    jid = r.json()['job_id']
    _wait_job(service, store, jid)

    conv = client.get(f'/api/v4/conversations/{cid}', headers=h).json()
    assert not _active_pending(conv), repr(conv['messages'])
    answered = [m for m in conv['messages'] if m.get('job_id') == jid and m.get('role') == 'assistant']
    assert len(answered) == 1, repr(answered)
    assert answered[0]['status'] == 'cancelled', '取消要有自己的终态，不能混成 failed：' + repr(answered[0])


def test_idempotent_replay_still_points_at_the_original_user_message(app_env, monkeypatch):
    """回执仍绑在原用户消息上：同 key 重发返回同一个 job，且不新增用户消息。"""
    client, store, service, repo = app_env
    h = login(client)
    aid = _agent(client, h, service, '报价助手')['id']
    cid = _do_convo(client, h, aid)

    monkeypatch.setattr(service.runner, 'run',
                        lambda req, emit, cancel: ProviderResult(text='ok'))

    first = client.post(f'/api/v4/conversations/{cid}/messages',
                        json={'content': '报价', 'idempotency_key': 'key-abcdef12'}, headers=h)
    assert first.status_code == 201, first.text
    jid = first.json()['job_id']
    _wait_job(service, store, jid)

    again = client.post(f'/api/v4/conversations/{cid}/messages',
                        json={'content': '报价', 'idempotency_key': 'key-abcdef12'}, headers=h)
    assert again.status_code == 201, again.text
    assert again.json().get('idempotent_replay') is True, again.json()
    assert again.json()['job_id'] == jid, again.json()

    conv = client.get(f'/api/v4/conversations/{cid}', headers=h).json()
    users = [m for m in conv['messages'] if m.get('role') == 'user']
    assert len(users) == 1, '重放不应新增用户消息：' + repr(users)


def test_answer_left_by_a_dead_process_is_ended_on_read(app_env, monkeypatch):
    """上一个进程留下的 pending，在读取时结束成中断——不查 job、不标成功。

    直接改存储行是对「重启后只剩这一行」的忠实模拟：活下来的正是它。
    """
    import json
    client, store, service, repo = app_env
    h = login(client)
    aid = _agent(client, h, service, '报价助手')['id']
    cid = _do_convo(client, h, aid)

    with store.connect() as db:
        row = db.execute('SELECT data FROM agent_conversations WHERE id=?', (cid,)).fetchone()
        c = json.loads(row[0])
        c['messages'].append({'id': 'orphan', 'role': 'assistant', 'content': '正在回答',
                              'status': 'pending', 'job_id': 'job-from-dead-process',
                              'created_at': c['created_at'], 'boot_id': 'a-previous-process'})
        db.execute('UPDATE agent_conversations SET data=? WHERE id=?',
                   (json.dumps(c, ensure_ascii=False), cid))

    conv = client.get(f'/api/v4/conversations/{cid}', headers=h).json()
    msg = next(m for m in conv['messages'] if m['id'] == 'orphan')
    assert msg['status'] == 'interrupted', msg
    assert msg['content'] == '服务重启，回答中断', msg
    assert not _active_pending(conv)


def test_read_does_not_touch_an_answer_from_the_live_process(app_env, monkeypatch):
    """本进程正在回答的 pending，读取时不得被动过（尤其不得标成功）。"""
    import json
    import factory.control.agent_routes as ar
    client, store, service, repo = app_env
    h = login(client)
    aid = _agent(client, h, service, '报价助手')['id']
    cid = _do_convo(client, h, aid)

    with store.connect() as db:
        row = db.execute('SELECT data FROM agent_conversations WHERE id=?', (cid,)).fetchone()
        c = json.loads(row[0])
        c['messages'].append({'id': 'live', 'role': 'assistant', 'content': '正在回答',
                              'status': 'pending', 'job_id': 'job-in-flight',
                              'created_at': c['created_at'],
                              'boot_id': ar._CHAT_PROCESS_BOOT})
        db.execute('UPDATE agent_conversations SET data=? WHERE id=?',
                   (json.dumps(c, ensure_ascii=False), cid))

    conv = client.get(f'/api/v4/conversations/{cid}', headers=h).json()
    msg = next(m for m in conv['messages'] if m['id'] == 'live')
    assert msg['status'] == 'pending', '读取动了本进程正在进行的回答：' + repr(msg)
    assert msg['content'] == '正在回答', msg
