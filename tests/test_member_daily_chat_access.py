"""普通成员的「无项目 do 日常会话」入口。

现场（f5b238c）：管理员报价验收通过，但普通 member 连
POST /api/v4/agents/{aid}/conversations mode=do 都被全局中间件 403。
共识是「管理员配置、成员使用」，日常聊天不该要管理员权限。

窄授权只放开成员完成自己这段对话所必需的动作，归属校验一律由路由处理器执行。
maintain（改角色）、项目型会话、admin-config、跨用户操作保持受限。
"""
from factory.control.providers import ProviderResult
from tests.test_admin_config_conversation import env, _login, _member, _wait_job  # noqa: F401


def _as(client, name):
    """重新以该成员登录。TestClient 共享 cookie，切换身份必须重新登录，
    否则旧 headers 里的 CSRF 会和新 cookie 对不上。"""
    return _login(client, name, 'member-long-password')


def _agent(client, headers, name='团队助手'):
    return client.post('/api/v4/agents', headers=headers,
                       json={'name': name, 'purpose': name}).json()['id']


# ---------- 正向：成员能走完自己的一段日常会话 ----------

def test_member_can_create_and_use_their_own_daily_chat(env, monkeypatch):
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    member = _member(client, 'daily-member')

    monkeypatch.setattr(service.runner, 'run',
                        lambda req, emit, cancel: ProviderResult('已按资料作答'))

    created = client.post(f'/api/v4/agents/{aid}/conversations',
                          headers=member, json={'mode': 'do'})
    assert created.status_code == 201, created.text
    cid = created.json()['id']

    sent = client.post(f'/api/v4/conversations/{cid}/messages',
                       headers=member, json={'content': '帮我算一下这单'})
    assert sent.status_code == 201, sent.text

    att = client.post(f'/api/v4/conversations/{cid}/attachments', headers=member,
                      files={'file': ('rule.txt', '单价 99.90，VIP 九折'.encode(), 'text/plain')})
    assert att.status_code == 201, att.text

    calc = client.post(f'/api/v4/conversations/{cid}/calc', headers=member, json={
        'items': [{'name': '标准服务', 'unit_price': '99.90', 'quantity': '3'}],
        'discount_rate': '0.9'})
    assert calc.status_code == 200, calc.text

    exported = client.post(f'/api/v4/conversations/{cid}/export', headers=member,
                           json={'title': '报价单', 'format': 'md', 'content': '# 报价'})
    assert exported.status_code == 201, exported.text
    eid = exported.json()['export']['id']
    dl = client.get(f'/api/v4/conversations/{cid}/exports/{eid}/download', headers=member)
    assert dl.status_code == 200, dl.text

    assert client.get(f'/api/v4/conversations/{cid}', headers=member).status_code == 200


def test_member_can_cancel_their_own_answer_job(env, monkeypatch):
    import threading
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    member = _member(client, 'cancel-member')
    gate = threading.Event()
    monkeypatch.setattr(service.runner, 'run',
                        lambda req, emit, cancel: (gate.wait(5), ProviderResult('ok'))[1])

    cid = client.post(f'/api/v4/agents/{aid}/conversations',
                      headers=member, json={'mode': 'do'}).json()['id']
    sent = client.post(f'/api/v4/conversations/{cid}/messages',
                       headers=member, json={'content': '慢慢算'})
    assert sent.status_code == 201, sent.text
    jid = sent.json()['job_id']
    try:
        cancelled = client.post(f'/api/v4/maintenance-jobs/{jid}/cancel', headers=member)
        assert cancelled.status_code == 200, cancelled.text
    finally:
        gate.set()
        _wait_job(service, jid)


# ---------- 反向：越权一律挡住 ----------

def test_member_cannot_cancel_another_users_job(env, monkeypatch):
    """job 取消的归属：service.cancel_maintenance 本身不校验 actor，必须在入口挡。"""
    import threading
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    _member(client, 'job-owner')
    _member(client, 'job-stranger')
    gate = threading.Event()
    monkeypatch.setattr(service.runner, 'run',
                        lambda req, emit, cancel: (gate.wait(5), ProviderResult('ok'))[1])

    owner = _as(client, 'job-owner')
    created = client.post(f'/api/v4/agents/{aid}/conversations',
                          headers=owner, json={'mode': 'do'})
    assert created.status_code == 201, created.text
    cid = created.json()['id']
    sent = client.post(f'/api/v4/conversations/{cid}/messages',
                       headers=owner, json={'content': '慢慢算'})
    assert sent.status_code == 201, sent.text
    jid = sent.json()['job_id']
    try:
        other = _as(client, 'job-stranger')
        r = client.post(f'/api/v4/maintenance-jobs/{jid}/cancel', headers=other)
        assert r.status_code == 403, (r.status_code, r.text)
    finally:
        gate.set()
        _wait_job(service, jid)


def test_member_cannot_touch_another_users_conversation(env):
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    _member(client, 'conv-owner')
    _member(client, 'conv-stranger')

    owner = _as(client, 'conv-owner')
    created = client.post(f'/api/v4/agents/{aid}/conversations',
                          headers=owner, json={'mode': 'do'})
    assert created.status_code == 201, created.text
    cid = created.json()['id']

    other = _as(client, 'conv-stranger')
    assert client.get(f'/api/v4/conversations/{cid}', headers=other).status_code == 403
    assert client.post(f'/api/v4/conversations/{cid}/messages', headers=other,
                       json={'content': '偷看'}).status_code == 403
    assert client.post(f'/api/v4/conversations/{cid}/export', headers=other,
                       json={'title': 'x', 'format': 'md', 'content': 'x'}).status_code == 403


def test_member_cannot_open_a_maintain_or_project_conversation(env):
    """maintain 会改角色；项目型会话要走项目授权。两者都不在这道窄口里。"""
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    member = _member(client, 'scope-member')

    m = client.post(f'/api/v4/agents/{aid}/conversations', headers=member,
                    json={'mode': 'maintain'})
    assert m.status_code == 403, (m.status_code, m.text)

    p = client.post(f'/api/v4/agents/{aid}/conversations', headers=member,
                    json={'mode': 'do', 'project_id': 'p-any'})
    assert p.status_code == 403, (p.status_code, p.text)


def test_member_still_blocked_on_admin_config_and_other_v4_writes(env):
    """窄口不得外溢：admin-config、角色编辑、能力导入仍是 admin-only。"""
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    member = _member(client, 'blocked-member')

    assert client.post('/api/v4/admin-config/conversations', headers=member,
                       json={}).status_code == 403
    assert client.post('/api/v4/agents', headers=member,
                       json={'name': 'x', 'purpose': 'x'}).status_code == 403
    assert client.post(f'/api/v4/agents/{aid}/rollback', headers=member,
                       json={'version': 1}).status_code == 403
    assert client.post('/api/v4/modules', headers=member,
                       json={'name': 'x', 'category': 'style', 'instructions': 'x'}).status_code == 403


def test_member_cannot_retry_or_bind_a_project_on_a_conversation(env):
    """retry 只属于维护对话；project 绑定要走项目授权。都不在窄口内。"""
    client, store, service = env
    admin = _login(client)
    aid = _agent(client, admin)
    member = _member(client, 'retry-member')
    cid = client.post(f'/api/v4/agents/{aid}/conversations',
                      headers=member, json={'mode': 'do'}).json()['id']

    assert client.post(f'/api/v4/conversations/{cid}/retry', headers=member).status_code == 403
    assert client.post(f'/api/v4/conversations/{cid}/project', headers=member,
                       json={'project_id': 'p1'}).status_code == 403
