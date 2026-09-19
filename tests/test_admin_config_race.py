"""配置对话的并发/恢复边界。

Codex 在 274de00 上确定性复现了「轮询覆盖刚完成的回答」。修复把所有会话写入
收敛到一个带 BEGIN IMMEDIATE 的原子读-改-写入口，这里锁住修复的四个边界：

1. 派发窗口（pending 已落库、job 尚未注册）不得被当成「任务记录丢失」。
2. 真实重启留下的 pending 仍要被恢复成中断。
3. job 报 completed 但回答没能落库时，不得伪装成成功。
4. 恢复写回时不得覆盖并发写入的新消息。
"""
import json
import threading

import pytest

from tests.test_admin_config_conversation import env, _login  # noqa: F401
from factory.control.providers import ProviderResult


def _start_conv(client, headers):
    return client.post('/api/v4/admin-config/conversations',
                       headers=headers, json={}).json()['id']


def _row(store, cid):
    with store.connect() as db:
        return json.loads(db.execute(
            'SELECT data FROM admin_config_conversations WHERE id=?', (cid,)).fetchone()[0])


def _write_row(store, conv):
    with store.connect() as db:
        db.execute('UPDATE admin_config_conversations SET data=? WHERE id=?',
                   (json.dumps(conv, ensure_ascii=False), conv['id']))


def test_dispatch_window_not_misjudged_as_interrupted(env, monkeypatch):
    """pending 已落库但 start_maintenance 还没返回时，GET 必须仍看到 pending。

    这是真实窗口：消息先落库才派发，所以两者之间必然存在一段 job 查不到的时间。
    旧代码在这里直接判 KeyError -> 「任务记录丢失」。
    """
    client, store, service = env
    h = _login(client)
    cid = _start_conv(client, h)

    observed = {}
    real_start = service.start_maintenance

    def start_but_poll_first(fn, *, job_id, **kw):
        # 此刻：pending 已落库，job 尚未注册，dispatch 仍是 queued。
        observed['during_dispatch'] = client.get(
            f'/api/v4/admin-config/conversations/{cid}', headers=h).json()
        return real_start(fn, job_id=job_id, **kw)

    monkeypatch.setattr(service, 'start_maintenance', start_but_poll_first)
    monkeypatch.setattr(service.runner, 'run',
                        lambda req, emit, cancel: ProviderResult('ok'))

    r = client.post(f'/api/v4/admin-config/conversations/{cid}/messages',
                    headers=h, json={'content': '读取配置'})
    assert r.status_code == 201

    during = observed['during_dispatch']
    assistant = [m for m in during['messages'] if m.get('role') == 'assistant']
    assert assistant, '派发窗口内应已能看到 pending 消息'
    assert assistant[0]['status'] == 'pending', assistant[0]
    assert assistant[0]['content'] != '任务记录丢失', assistant[0]


def test_real_restart_still_recovers_as_interrupted(env, monkeypatch):
    """上一个进程留下的 pending（boot_id 不同、job 查不到）仍要恢复成中断。

    直接改存储行是对「重启后只剩这一行」的忠实模拟——重启后活下来的正是它。
    """
    client, store, service = env
    h = _login(client)
    cid = _start_conv(client, h)

    conv = _row(store, cid)
    conv['messages'].append({
        'id': 'm-old', 'role': 'assistant', 'content': '正在回答',
        'status': 'pending', 'job_id': 'job-from-previous-process',
        'created_at': conv['created_at'],
        'boot_id': 'a-different-process', 'dispatch': 'registered',
    })
    _write_row(store, conv)

    def missing(job_id):
        raise KeyError(job_id)

    monkeypatch.setattr(service, 'maintenance_status', missing)

    got = client.get(f'/api/v4/admin-config/conversations/{cid}', headers=h).json()
    msg = next(m for m in got['messages'] if m['id'] == 'm-old')
    assert msg['status'] == 'interrupted', msg
    assert msg['content'] == '服务重启，任务中断', msg


def test_completed_job_without_saved_answer_is_not_reported_as_success(env, monkeypatch):
    """job 说 completed 但回答没落库时，不得把占位文案标成成功。"""
    client, store, service = env
    h = _login(client)
    cid = _start_conv(client, h)

    conv = _row(store, cid)
    conv['messages'].append({
        'id': 'm-lost', 'role': 'assistant', 'content': '正在回答',
        'status': 'pending', 'job_id': 'job-answer-lost',
        'created_at': conv['created_at'],
        'boot_id': 'a-different-process', 'dispatch': 'registered',
    })
    _write_row(store, conv)

    monkeypatch.setattr(service, 'maintenance_status',
                        lambda job_id: {'status': 'completed'})

    got = client.get(f'/api/v4/admin-config/conversations/{cid}', headers=h).json()
    msg = next(m for m in got['messages'] if m['id'] == 'm-lost')
    assert msg['status'] != 'completed', '回答丢失不能报成功：' + repr(msg)
    assert msg['status'] == 'interrupted', msg
    assert msg['content'] == '任务已结束，但回答未能保存', msg


def test_reconcile_does_not_drop_a_message_added_concurrently(env, monkeypatch):
    """恢复写回时，不能把查询期间新增的消息一起抹掉。

    旧实现写回的是查询前读到的整份 JSON，任何并发新增都会消失。
    """
    client, store, service = env
    h = _login(client)
    cid = _start_conv(client, h)

    conv = _row(store, cid)
    conv['messages'].append({
        'id': 'm-stale', 'role': 'assistant', 'content': '正在回答',
        'status': 'pending', 'job_id': 'job-stale',
        'created_at': conv['created_at'],
        'boot_id': 'a-different-process', 'dispatch': 'registered',
    })
    _write_row(store, conv)

    def status_then_insert(job_id):
        # 模拟查询期间另一个写入者提交了一条新消息。
        latest = _row(store, cid)
        latest['messages'].append({
            'id': 'm-concurrent', 'role': 'user', 'content': '并发写入的新消息',
            'created_at': latest['created_at'],
        })
        _write_row(store, latest)
        return {'status': 'interrupted'}

    monkeypatch.setattr(service, 'maintenance_status', status_then_insert)

    got = client.get(f'/api/v4/admin-config/conversations/{cid}', headers=h).json()
    ids = [m['id'] for m in got['messages']]
    assert 'm-concurrent' in ids, '恢复写回抹掉了并发新增的消息：' + repr(ids)
    assert next(m for m in got['messages'] if m['id'] == 'm-stale')['status'] == 'interrupted'


def test_stale_missing_job_evidence_is_invalidated_by_stage_change(env, monkeypatch):
    """查询期间派发阶段前进后，旧的「查不到 job」不得用来终结新状态。

    确定性构造：maintenance_status 抛 KeyError 的同时，把该消息的 dispatch 从
    queued 推进到 registered——正是真实时序里 POST 在 GET 查询期间完成注册。
    此时那条否定证据描述的已是过去的阶段，必须失效、留待下次轮询重新观察。
    """
    client, store, service = env
    h = _login(client)
    cid = _start_conv(client, h)

    conv = _row(store, cid)
    conv['messages'].append({
        'id': 'm-dispatching', 'role': 'assistant', 'content': '正在回答',
        'status': 'pending', 'job_id': 'job-being-registered',
        'created_at': conv['created_at'],
        'boot_id': None, 'dispatch': 'queued',
    })
    _write_row(store, conv)
    # 与本进程同一 boot_id，模拟本进程正在派发的消息。
    import factory.control.agent_routes as ar  # noqa: F401  (文档用途)

    def missing_then_register(job_id):
        latest = _row(store, cid)
        for m in latest['messages']:
            if m.get('job_id') == job_id:
                m['dispatch'] = 'registered'
        _write_row(store, latest)
        raise KeyError(job_id)

    monkeypatch.setattr(service, 'maintenance_status', missing_then_register)

    got = client.get(f'/api/v4/admin-config/conversations/{cid}', headers=h).json()
    msg = next(m for m in got['messages'] if m['id'] == 'm-dispatching')
    assert msg['status'] == 'pending', '过期的否定观察终结了已注册的任务：' + repr(msg)
    assert msg['content'] == '正在回答', msg
