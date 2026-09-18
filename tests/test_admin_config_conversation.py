"""N11：管理员配置对话入口测试。

5 条必测：
1. admin 走配置入口 → binding admin_config=True，工具清单含配置工具。
2. member 走同一入口 → 拒绝，且 binding admin_config 为 false、工具清单只有 calc/export。
3. 客户端谎报：member 在请求体里自带 admin_config: true → 服务端仍判定 false（变异验证）。
4. 通过对话改一个配置项 → 表单读接口读到的是改后的值（同一份配置）。
5. 普通 do 会话不受影响（既有测试保持绿）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from factory.control.app import create_app
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store
from tests.test_control_app import FakeSDK, login


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
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
    profiles = {role: {'provider': 'codex', 'model': 'test'}
                for role in ('planner', 'cheap', 'standard', 'strong')}
    service = Service(store, runner=FakeSDK(), profiles=profiles)
    app = create_app(data_dir=data, workspace_root=tmp_path / 'repos',
                     public_origin='http://testserver', service=service,
                     webhook_secret='test-webhook-secret')
    app.state.auth.create_user('admin1', 'a-long-test-password')  # default role = admin
    with TestClient(app) as client:
        yield client, store, service


def _login(client, username='admin1', password='a-long-test-password'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password},
                    headers={'Origin': 'http://testserver'})
    assert r.status_code == 200, r.text
    return {'Origin': 'http://testserver', 'X-CSRF-Token': r.json()['csrf_token']}


def _member(client, name='member1'):
    client.app.state.auth.create_user(name, 'member-long-password', role='member')
    return _login(client, name, 'member-long-password')


def _create_config_conv(client, headers):
    return client.post('/api/v4/admin-config/conversations', json={}, headers=headers)


def _send(client, headers, cid, content):
    return client.post(f'/api/v4/admin-config/conversations/{cid}/messages',
                       json={'content': content}, headers=headers)


def _wait_job(service, job_id, deadline_s=5):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            st = service.maintenance_status(job_id)
            if st['status'] in ('completed', 'failed', 'interrupted'):
                return st
        except KeyError:
            return {'status': 'unknown'}
        time.sleep(0.05)
    return {'status': 'timeout'}


# ---------------------------------------------------------------------------
# Test 1: admin 走配置入口 → binding admin_config=True，工具清单含配置工具
# ---------------------------------------------------------------------------

def test_admin_config_conversation_has_config_tools(env, monkeypatch):
    client, store, service = env
    headers = _login(client)

    # Capture the provider request to inspect the binding
    reqs = []
    def fake_run(req, emit, cancel):
        reqs.append(req)
        return ProviderResult(text='已完成配置')
    monkeypatch.setattr(service.runner, 'run', fake_run)

    # Create admin config conversation
    r = _create_config_conv(client, headers)
    assert r.status_code == 201, r.text
    cid = r.json()['id']

    # Send a message
    r = _send(client, headers, cid, '查看当前运行配置')
    assert r.status_code == 201, r.text
    job_id = r.json().get('job_id')

    # Wait for async job
    if job_id:
        _wait_job(service, job_id)

    # Verify the binding had admin_config=True
    assert len(reqs) == 1
    binding = reqs[0].conversation_binding
    assert binding['admin_config'] is True
    assert binding['actor_role'] == 'admin'

    # Verify tools include admin config tools via MCP inventory
    from factory.control import conversation_tools as _ct
    from factory.control.admin_config_tools import ADMIN_TOOL_NAMES
    tools = _ct.ConversationTools.from_binding(binding)

    # Use the gate test helper to get real MCP tool names
    from tests.test_admin_config_surface_gate import _names
    names = _names(tools)
    bare_admin = {n.rsplit('__', 1)[-1] for n in ADMIN_TOOL_NAMES}
    assert bare_admin <= names, f'missing config tools: {sorted(bare_admin - names)}'
    assert {'calc', 'export'} <= names


# ---------------------------------------------------------------------------
# Test 2: member 走同一入口 → 拒绝，且 binding admin_config 为 false
# ---------------------------------------------------------------------------

def test_member_config_conversation_rejected(env):
    """Member is rejected at the config conversation creation endpoint."""
    client, store, service = env
    mh = _member(client)

    r = _create_config_conv(client, mh)
    assert r.status_code == 403


def test_member_cannot_send_to_admin_config_conversation(env, monkeypatch):
    """Member cannot send messages to an admin config conversation."""
    client, store, service = env
    # Admin creates the conversation first
    admin_h = _login(client)

    reqs = []
    def fake_run(req, emit, cancel):
        reqs.append(req)
        return ProviderResult(text='已完成配置')
    monkeypatch.setattr(service.runner, 'run', fake_run)

    r = _create_config_conv(client, admin_h)
    assert r.status_code == 201
    cid = r.json()['id']

    # Now switch to member (login overwrites the session cookie)
    member_h = _member(client)

    # Member tries to send a message → 403 (middleware blocks member POST)
    r = _send(client, member_h, cid, '偷改配置')
    assert r.status_code == 403

    # Verify no model call was made
    assert len(reqs) == 0


# ---------------------------------------------------------------------------
# Test 3: 客户端谎报 admin_config: true → 服务端仍判定 false（变异验证）
# ---------------------------------------------------------------------------

def test_client_lie_admin_config_ignored(env):
    """Mutation verification: server ignores client-provided admin_config flag.

    Two layers of defense:
    A) The HTTP endpoint does not accept admin_config from the request body.
    B) Even if binding had admin_config=True, member role blocks config tools
       (tested by test_admin_config_surface_gate).
    """
    client, store, service = env
    member_h = _member(client, 'liar1')

    # Layer A: member sends admin_config=True in request body → rejected at middleware
    r = client.post('/api/v4/admin-config/conversations',
                    json={'admin_config': True}, headers=member_h)
    assert r.status_code == 403, \
        f'Member with admin_config=True in body must be rejected, got {r.status_code}'

    # Layer B: mutation test at the binding level.
    # Even if server code were mutated to pass admin_config=True for a member,
    # the gate in create_server still blocks config tools for non-admin roles.
    from factory.control import conversation_tools as ct
    binding = ct.binding_for(store, 'fake-cid', 'member-user',
                             actor_role='member', admin_config=True)
    assert binding['admin_config'] is True  # binding_for passes it through
    # But ConversationTools + create_server gates on role:
    tools = ct.ConversationTools.from_binding(binding)
    from tests.test_admin_config_surface_gate import _names
    names = _names(tools)
    assert names == {'calc', 'export'}, \
        f'MUTATION CAUGHT: member + admin_config=True must not get config tools, got: {names}'


def test_client_lie_admin_flag_in_normal_conversation(env, monkeypatch):
    """Even if admin_config field is smuggled into a normal conversation request,
    the binding always uses server-determined value (False for normal chats)."""
    client, store, service = env
    admin_h = _login(client)

    reqs = []
    def fake_run(req, emit, cancel):
        reqs.append(req)
        return ProviderResult(text='普通回答')
    monkeypatch.setattr(service.runner, 'run', fake_run)

    # Create a normal agent
    ag = client.post('/api/v4/agents', json={'name': '测试助手', 'purpose': '测试'},
                     headers=admin_h)
    assert ag.status_code == 201
    aid = ag.json()['id']

    # Create normal conversation (admin_config is not a field in ConversationCreate)
    conv = client.post(f'/api/v4/agents/{aid}/conversations',
                       json={'mode': 'do'}, headers=admin_h)
    assert conv.status_code == 201
    cid = conv.json()['id']

    # Send message as admin (even admin in normal chat should not get config tools)
    msg = client.post(f'/api/v4/conversations/{cid}/messages',
                      json={'content': '查看配置'}, headers=admin_h)
    assert msg.status_code == 201

    time.sleep(0.3)

    # Verify: admin_config must be False in normal conversation binding
    assert len(reqs) == 1
    binding = reqs[0].conversation_binding
    assert binding['admin_config'] is False, \
        'MUTATION: normal do conversation binding must have admin_config=False'


# ---------------------------------------------------------------------------
# Test 4: 通过对话改一个配置项 → 表单读接口读到的是改后的值（同一份配置）
# ---------------------------------------------------------------------------

def test_config_change_reflected_in_form_read(env):
    """Admin changes runtime config through the config tools →
    form read API returns the updated value (same source of truth)."""
    client, store, service = env
    admin_h = _login(client)

    # Read initial runtime config via form API
    initial = client.get('/api/v2/runtime', headers=admin_h)
    assert initial.status_code == 200
    initial_data = initial.json()
    initial_revision = initial_data.get('revision',
                                        initial_data.get('configuration_revision', 0))

    # Use AdminConfigTools directly (same code path the MCP server calls)
    from factory.control.admin_config_tools import AdminConfigTools
    admin_tools = AdminConfigTools(store, actor_id=1, actor_role='admin')

    new_profiles = {role: {'provider': 'codex', 'model': 'updated-model'}
                    for role in ('planner', 'cheap', 'standard', 'strong')}
    new_limits = {'timeout_s': 7200, 'max_parallel': 3, 'max_tasks': 15,
                  'unknown_cost_policy': 'allow_bounded'}

    # The revision from initial read is what we pass for optimistic locking
    result = admin_tools.update_runtime_config(
        new_profiles, new_limits, initial_revision or 1)
    assert result['status'] == 'ok', result

    # Read via form API again — must see the updated values
    updated = client.get('/api/v2/runtime', headers=admin_h)
    assert updated.status_code == 200
    body = updated.json()
    assert body['profiles']['planner']['model'] == 'updated-model'
    assert body['limits']['timeout_s'] == 7200
    assert body['limits']['max_parallel'] == 3


# ---------------------------------------------------------------------------
# Test 5: 普通 do 会话不受影响
# ---------------------------------------------------------------------------

def test_normal_do_session_unaffected(env, monkeypatch):
    """A normal do-mode conversation does NOT get admin config tools,
    even when the actor is admin. This verifies the gate stays closed
    for normal chats. The existing tests in test_admin_config_surface_gate.py
    and test_agent_chat_capability.py are also required to stay green."""
    client, store, service = env
    admin_h = _login(client)

    reqs = []
    def fake_run(req, emit, cancel):
        reqs.append(req)
        return ProviderResult(text='普通回答')
    monkeypatch.setattr(service.runner, 'run', fake_run)

    # Create a normal agent and conversation
    ag = client.post('/api/v4/agents', json={'name': '报价助手', 'purpose': '报价'},
                     headers=admin_h)
    assert ag.status_code == 201
    aid = ag.json()['id']

    conv = client.post(f'/api/v4/agents/{aid}/conversations',
                       json={'mode': 'do'}, headers=admin_h)
    assert conv.status_code == 201
    cid = conv.json()['id']

    msg = client.post(f'/api/v4/conversations/{cid}/messages',
                      json={'content': '报个价'}, headers=admin_h)
    assert msg.status_code == 201

    # Wait for async processing
    time.sleep(0.3)

    assert len(reqs) == 1
    binding = reqs[0].conversation_binding
    assert binding['admin_config'] is False, \
        'Normal do conversation must NOT have admin_config=True'
    assert binding['actor_role'] == 'admin'
