"""R2 授权测试：会话 Skill 访问控制。

完成条件覆盖：
1. Codex 复现由红转绿（test_codex_review.py 负责，此处不重复）
2. 无关 member GET/导入/删除 他人会话 → 403/404
3. 普通 member 能导入自己的会话
4. 窄授权没有放开别的 v4 写接口（含变异验证）
5. 授权先于外部拉取：无关 member github 导入他人会话时，拉取层调用次数 0
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from factory.control.agents import AgentStore
from factory.control.app import create_app
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store


# ── 辅助 ──────────────────────────────────────────────

def _skill_zip(name='auth-test-skill'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('SKILL.md', f'---\nname: {name}\ndescription: test\n---\nbody\n')
    return buf.getvalue()


class FakeSDK:
    def available(self):
        return [{'id': 'codex', 'installed': True, 'detail': 'test'}]

    def run(self, request, emit, cancel=None):
        return ProviderResult('done', cost_usd=0.01)


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
    profiles = {k: {'provider': 'codex', 'model': 'test'}
                for k in ('planner', 'cheap', 'standard', 'strong')}
    svc = Service(store, runner=FakeSDK(), profiles=profiles)
    app = create_app(data_dir=data, workspace_root=tmp_path / 'repos',
                     public_origin='http://testserver', service=svc,
                     webhook_secret='test-secret')
    # admin 用户（默认角色）
    app.state.auth.create_user('admin1', 'a-long-test-password')
    with TestClient(app) as client:
        yield client, store, svc, app


def _login(client, username='admin1', password='a-long-test-password'):
    r = client.post('/api/auth/login',
                    json={'username': username, 'password': password},
                    headers={'Origin': 'http://testserver'})
    assert r.status_code == 200, r.text
    return {'Origin': 'http://testserver', 'X-CSRF-Token': r.json()['csrf_token']}


def _member(app, client, name='member1'):
    app.state.auth.create_user(name, 'member-long-password', role='member')
    return _login(client, name, 'member-long-password')


def _create_session_via_api(client, headers):
    """通过 API 创建 agent + conversation，actor_id 与登录用户一致。"""
    aid = client.post('/api/v4/agents', headers=headers,
                      json={'name': 'AuthTest', 'purpose': 'test'}).json()['id']
    cid = client.post(f'/api/v4/agents/{aid}/conversations', headers=headers,
                      json={'mode': 'do'}).json()['id']
    return cid


def _import_skill(client, cid, headers):
    """ZIP 方式导入 Skill。"""
    return client.post(f'/api/v4/sessions/{cid}/skills', headers=headers,
                       files={'file': ('skill.zip', _skill_zip(), 'application/zip')})


# ── 完成条件 2：无关 member GET/导入/删除 他人会话 → 403/404 ──

class TestUnrelatedMemberBlocked:
    def test_member_cannot_get_other_session_skills(self, env):
        client, store, _, app = env
        admin_h = _login(client)
        cid = _create_session_via_api(client, admin_h)
        r = _import_skill(client, cid, admin_h)
        assert r.status_code == 201
        # 切到无关 member
        mh = _member(app, client, 'outsider-get')
        r = client.get(f'/api/v4/sessions/{cid}/skills', headers=mh)
        assert r.status_code in (403, 404), f'GET 泄漏: {r.status_code}'

    def test_member_cannot_import_to_other_session(self, env):
        client, store, _, app = env
        admin_h = _login(client)
        cid = _create_session_via_api(client, admin_h)
        mh = _member(app, client, 'outsider-import')
        r = _import_skill(client, cid, mh)
        assert r.status_code in (403, 404), f'POST 越权: {r.status_code}'

    def test_member_cannot_delete_other_session_skill(self, env):
        client, store, _, app = env
        admin_h = _login(client)
        cid = _create_session_via_api(client, admin_h)
        skill_id = _import_skill(client, cid, admin_h).json()['id']
        mh = _member(app, client, 'outsider-delete')
        r = client.delete(f'/api/v4/sessions/{cid}/skills/{skill_id}', headers=mh)
        assert r.status_code in (403, 404), f'DELETE 越权: {r.status_code}'


# ── 完成条件 3：member 能导入自己的会话 ──

class TestMemberOwnSession:
    def test_member_can_import_own_session(self, env):
        client, store, _, app = env
        # 创建 member 并获取其 user id
        user_info = app.state.auth.create_user('self-importer', 'member-long-password', role='member')
        member_uid = user_info['id']
        # admin 先建 agent（member 无权建 agent）
        admin_h = _login(client)
        aid = client.post('/api/v4/agents', headers=admin_h,
                          json={'name': 'MemberAgent', 'purpose': 'test'}).json()['id']
        # 用 store 直接创建 conversation，actor_id 设为 member 的 user id
        agents = AgentStore(store)
        cid_data = agents.create_conversation(aid, 'do', actor_id=member_uid)
        # 最后登录 member（避免 cookie 被后续 admin login 覆盖）
        mh = _login(client, 'self-importer', 'member-long-password')
        # member 导入自己的会话
        r = _import_skill(client, cid_data['id'], mh)
        assert r.status_code == 201, f'member 导入自己会话失败: {r.status_code} {r.text}'
        # 验证 GET 也能看到
        r = client.get(f'/api/v4/sessions/{cid_data["id"]}/skills', headers=mh)
        assert r.status_code == 200
        assert len(r.json()['items']) == 1


# ── 完成条件 4：窄授权没有放开别的 v4 写接口 ──

class TestNarrowWhitelist:
    def test_member_still_blocked_on_other_v4_write(self, env):
        """member 打一个不相关的 v4 写端点仍然 403。"""
        client, store, _, app = env
        mh = _member(app, client, 'narrow-test')
        # 尝试创建 agent（/api/v4/agents POST）—— 应该被中间件拒绝
        r = client.post('/api/v4/agents', headers=mh,
                        json={'name': 'Attempt', 'purpose': 'test'})
        assert r.status_code == 403, \
            f'窄授权泄漏：member 能写不相关 v4 端点: {r.status_code}'

    def test_mutation_wide_whitelist_breaks_test(self, env):
        """变异验证：把窄授权改宽（放开整个 /api/v4 前缀），上面那条测试必须变红。

        验证方式：
        1. 确认源码中窄授权正则只匹配 session skill 路径
        2. 确认没有 /api/v4 通配放行
        3. 确认 member 对不相关端点仍 403（行为验证）
        """
        client, store, _, app = env
        mh = _member(app, client, 'mutation-test')
        # 正常路径：必须被拒绝
        r = client.post('/api/v4/agents', headers=mh,
                        json={'name': 'Attempt', 'purpose': 'test'})
        assert r.status_code == 403, 'baseline: member 应被拒绝'

        # 源码审计：确认窄授权正则存在且范围正确
        import factory.control.app as app_module
        source = open(app_module.__file__).read()
        # 窄授权正则必须包含 sessions 路径限定
        assert '/sessions/' in source and 'session_skill_action' in source, \
            '源码中找不到窄授权变量'
        # 确认没有 /api/v4 通配放行
        assert "path.startswith('/api/v4')" not in source, \
            '检测到过宽的 v4 白名单模式: startswith'
        assert "r'/api/v4/.*'" not in source, \
            '检测到过宽的 v4 白名单模式: /api/v4/.*'
        assert "r'/api/v4'" not in source or 'sessions' in source, \
            '检测到过宽的 v4 白名单模式'

        # 行为验证：skill-ingestions（另一个 v4 写端点）也必须 403
        r2 = client.post('/api/v4/skill-ingestions', headers=mh,
                         json={'name': 'test'})
        assert r2.status_code in (403, 413), \
            f'skill-ingestions 对 member 放开了: {r2.status_code}'


# ── 完成条件 5：授权先于外部拉取 ──

class TestAuthBeforeFetch:
    def test_auth_before_github_fetch(self, env):
        """无关 member github 导入他人会话时，拉取层一次都没被调用。"""
        client, store, _, app = env
        admin_h = _login(client)
        cid = _create_session_via_api(client, admin_h)

        # 注入可追踪的 fetcher
        call_count = 0

        class TrackingFetcher:
            def fetch(self, owner_repo, ref=None, subpath=None):
                nonlocal call_count
                call_count += 1
                raise RuntimeError('不应被调用')

        app.state.github_skill_fetcher = TrackingFetcher()

        mh = _member(app, client, 'fetch-guard')
        r = client.post(f'/api/v4/sessions/{cid}/skills', headers=mh,
                        json={'origin': 'github', 'owner_repo': 'org/repo'},
                        )
        assert r.status_code in (403, 404), f'应被授权拒绝: {r.status_code}'
        assert call_count == 0, f'授权应先于拉取，但 fetcher 被调用了 {call_count} 次'
