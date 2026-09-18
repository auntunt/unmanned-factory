"""GitHub 来源的会话 Skill 导入测试（N4）。

覆盖任务单 5 条必测：
1. fixture 拉取成功 → origin=github、source_version 是 40 位 sha、import_state=imported
2. 只给分支名 → 存的是解析后的 sha，不是分支名
3. 拉取失败 / 格式不合规 → rejected + 结构化缺口，表里没有成功记录
4. github 来源的绑定同样不进团队库、不授予权限（复用 N3 的断言口径）
5. 会话隔离对 github 来源同样成立
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import io
import zipfile

import pytest
import httpx

from factory.control.store import Store
from factory.control.agents import AgentStore
from factory.control.agent_manifests import ManifestStore
from factory.control.modules import ModuleStore
from factory.control.session_skills import SessionSkillStore
from factory.control.github_skill_fetch import (
    GitHubSkillFetcher,
    GitHubSkillFetchError,
    FetchResult,
    _parse_skill_content,
    SHA_RE,
)


# ── Fixtures ─────────────────────────────────────────

FAKE_SHA = 'a' * 40
FAKE_SHA_2 = 'b' * 40

SKILL_MD_CONTENT = b"""\
---
name: test-github-skill
description: A skill from GitHub
version: "2.1"
dependencies:
  - tool-alpha
  - tool-beta
---

# Test Skill

This is a test skill body.
"""

SKILL_MD_NO_NAME = b"""\
---
description: No name here
---

Body content.
"""

SKILL_MD_MINIMAL = b"""\
---
name: minimal-skill
---

Minimal skill.
"""


def _skill_md_b64(content: bytes = SKILL_MD_CONTENT) -> str:
    return base64.b64encode(content).decode()


def _make_github_contents_response(path: str, content: bytes) -> dict:
    """模拟 GitHub Contents API 响应。"""
    return {
        'type': 'file',
        'name': path.split('/')[-1],
        'path': path,
        'size': len(content),
        'content': base64.b64encode(content).decode(),
        'encoding': 'base64',
    }


class FakeTransport(httpx.BaseTransport):
    """可配置的假 HTTP 传输层，用于确定性测试。"""

    def __init__(self):
        self.responses: dict[str, httpx.Response] = {}

    def add(self, method: str, path: str, status: int, body=None, text=None):
        key = f'{method.upper()} {path}'
        if text is not None:
            self.responses[key] = httpx.Response(status, text=text)
        elif body is not None:
            self.responses[key] = httpx.Response(status, json=body)
        else:
            self.responses[key] = httpx.Response(status)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # 取相对路径（去掉 base_url）
        path = request.url.raw_path.decode()
        # 去掉查询参数匹配
        path_no_query = path.split('?')[0]
        key = f'{request.method} {path_no_query}'
        if key in self.responses:
            return self.responses[key]
        # 也尝试带查询参数的
        key_full = f'{request.method} {path}'
        if key_full in self.responses:
            return self.responses[key_full]
        return httpx.Response(404, json={'message': 'Not Found'})


def _fake_fetcher(transport: FakeTransport | None = None) -> GitHubSkillFetcher:
    """创建使用假传输层的 fetcher。"""
    t = transport or FakeTransport()
    client = httpx.Client(base_url='https://api.github.com', transport=t)
    return GitHubSkillFetcher(token='fake-token', client=client)


def _setup_success_transport(
    owner_repo: str = 'owner/repo',
    ref: str | None = None,
    sha: str = FAKE_SHA,
    subpath: str | None = None,
    content: bytes = SKILL_MD_CONTENT,
) -> FakeTransport:
    """配置一个成功拉取的传输层。"""
    transport = FakeTransport()

    # 解析 ref → SHA
    target = ref or 'HEAD'
    transport.add('GET', f'/repos/{owner_repo}/commits/{target}',
                  200, text=sha)

    # 拉取 SKILL.md
    if subpath:
        skill_path = subpath.strip('/') + '/SKILL.md'
    else:
        skill_path = 'SKILL.md'
    transport.add('GET', f'/repos/{owner_repo}/contents/{skill_path}',
                  200, body=_make_github_contents_response(skill_path, content))

    return transport


def _store_with_conversation(tmp_path, conversation_id='sess-aaa'):
    """创建 Store 并手工插入 conversation 行。"""
    store = Store(tmp_path / 'control.db')
    agents = AgentStore(store)
    agent = agents.create({'name': 'Test Agent', 'purpose': 'test'}, 'test-actor')
    with store.connect() as db:
        db.execute('INSERT INTO agent_conversations VALUES (?,?,?)',
                   (conversation_id, agent['id'],
                    json.dumps({'id': conversation_id, 'agent_id': agent['id'],
                                'mode': 'do', 'messages': [],
                                'created_at': '2026-01-01T00:00:00Z',
                                'updated_at': '2026-01-01T00:00:00Z'})))
    return store, agent


def _count_table(store, table):
    with store.connect() as db:
        return db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


# ── 拉取层单元测试 ────────────────────────────────────

class TestGitHubSkillFetcher:
    """拉取层可注入、确定性测试。"""

    def test_resolve_commit_sha_from_branch(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/commits/main', 200, text=FAKE_SHA)
        fetcher = _fake_fetcher(transport)
        sha = fetcher.resolve_commit_sha('owner/repo', 'main')
        assert sha == FAKE_SHA
        assert SHA_RE.fullmatch(sha)

    def test_resolve_commit_sha_default_head(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/commits/HEAD', 200, text=FAKE_SHA)
        fetcher = _fake_fetcher(transport)
        sha = fetcher.resolve_commit_sha('owner/repo')
        assert sha == FAKE_SHA

    def test_resolve_commit_sha_already_sha(self):
        transport = FakeTransport()
        transport.add('GET', f'/repos/owner/repo/commits/{FAKE_SHA}', 200, text=FAKE_SHA)
        fetcher = _fake_fetcher(transport)
        sha = fetcher.resolve_commit_sha('owner/repo', FAKE_SHA)
        assert sha == FAKE_SHA

    def test_resolve_commit_sha_invalid_repo(self):
        fetcher = _fake_fetcher()
        with pytest.raises(GitHubSkillFetchError, match='无效 GitHub 仓库名'):
            fetcher.resolve_commit_sha('invalid-repo-no-slash')

    def test_resolve_commit_sha_repo_not_found(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/nonexist/commits/HEAD', 404,
                      body={'message': 'Not Found'})
        fetcher = _fake_fetcher(transport)
        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.resolve_commit_sha('owner/nonexist')
        assert exc_info.value.reason == 'repo_not_found'

    def test_fetch_skill_md_success(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/contents/SKILL.md', 200,
                      body=_make_github_contents_response('SKILL.md', SKILL_MD_CONTENT))
        fetcher = _fake_fetcher(transport)
        path, content = fetcher.fetch_skill_md('owner/repo', FAKE_SHA)
        assert path == 'SKILL.md'
        assert content == SKILL_MD_CONTENT

    def test_fetch_skill_md_with_subpath(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/contents/skills/my-skill/SKILL.md', 200,
                      body=_make_github_contents_response(
                          'skills/my-skill/SKILL.md', SKILL_MD_CONTENT))
        fetcher = _fake_fetcher(transport)
        path, content = fetcher.fetch_skill_md('owner/repo', FAKE_SHA, 'skills/my-skill')
        assert path == 'skills/my-skill/SKILL.md'
        assert content == SKILL_MD_CONTENT

    def test_fetch_skill_md_not_found(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/contents/SKILL.md', 404,
                      body={'message': 'Not Found'})
        fetcher = _fake_fetcher(transport)
        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.fetch_skill_md('owner/repo', FAKE_SHA)
        assert exc_info.value.reason == 'skill_not_found'

    def test_fetch_full_success(self):
        """完整拉取流程：分支名 → 解析为 SHA → 拉取内容 → 解析元数据。"""
        transport = _setup_success_transport(ref='main')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='main')

        assert isinstance(result, FetchResult)
        assert result.commit_sha == FAKE_SHA
        assert SHA_RE.fullmatch(result.commit_sha)
        assert result.source_ref == 'owner/repo#main'
        assert result.entry == 'SKILL.md'
        assert result.name == 'test-github-skill'
        assert result.description == 'A skill from GitHub'
        assert result.version == '2.1'
        assert result.dependencies == ['tool-alpha', 'tool-beta']
        assert result.content_sha256 == hashlib.sha256(SKILL_MD_CONTENT).hexdigest()

    def test_fetch_with_subpath(self):
        transport = _setup_success_transport(
            ref='v1.0', subpath='skills/my-skill')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='v1.0', subpath='skills/my-skill')

        assert result.commit_sha == FAKE_SHA
        assert result.source_ref == 'owner/repo#v1.0/skills/my-skill'
        assert result.entry == 'skills/my-skill/SKILL.md'

    def test_fetch_no_ref_uses_head(self):
        transport = _setup_success_transport()  # no ref → HEAD
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo')

        assert result.commit_sha == FAKE_SHA
        assert result.source_ref == 'owner/repo'

    def test_fetch_invalid_skill_format(self):
        """SKILL.md 没有 name frontmatter 且文件名不是 SKILL.md → 格式不合规。"""
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/commits/HEAD', 200, text=FAKE_SHA)
        transport.add('GET', '/repos/owner/repo/contents/readme.md', 200,
                      body=_make_github_contents_response('readme.md', SKILL_MD_NO_NAME))
        fetcher = _fake_fetcher(transport)
        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.fetch('owner/repo', subpath='readme.md')
        assert exc_info.value.reason == 'invalid_skill_format'

    def test_fetch_auth_failure(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/commits/HEAD', 401,
                      body={'message': 'Bad credentials'})
        fetcher = _fake_fetcher(transport)
        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.fetch('owner/repo')
        assert exc_info.value.reason == 'auth_invalid'

    def test_fetch_access_denied(self):
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/commits/HEAD', 403,
                      body={'message': 'Forbidden'})
        fetcher = _fake_fetcher(transport)
        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.fetch('owner/repo')
        assert exc_info.value.reason == 'access_denied'


# ── _parse_skill_content 单元测试 ─────────────────────

class TestParseSkillContent:
    def test_valid_skill_md(self):
        result = _parse_skill_content('SKILL.md', SKILL_MD_CONTENT)
        assert result is not None
        assert result['name'] == 'test-github-skill'
        assert result['description'] == 'A skill from GitHub'
        assert result['version'] == '2.1'
        assert result['dependencies'] == ['tool-alpha', 'tool-beta']

    def test_skill_md_without_name_but_is_skill_md(self):
        """文件名是 SKILL.md 时即使没有 name frontmatter 也有效。"""
        content = b'---\ndescription: No name\n---\nBody\n'
        result = _parse_skill_content('SKILL.md', content)
        assert result is not None
        # name 从目录名推导
        assert result['name'] == 'root'

    def test_non_skill_md_without_name_returns_none(self):
        result = _parse_skill_content('readme.md', SKILL_MD_NO_NAME)
        assert result is None

    def test_non_skill_md_with_name_is_valid(self):
        content = b'---\nname: custom-skill\n---\nBody\n'
        result = _parse_skill_content('custom.md', content)
        assert result is not None
        assert result['name'] == 'custom-skill'

    def test_minimal_skill(self):
        result = _parse_skill_content('SKILL.md', SKILL_MD_MINIMAL)
        assert result is not None
        assert result['name'] == 'minimal-skill'
        assert result['dependencies'] == []

    def test_binary_content_returns_none(self):
        result = _parse_skill_content('SKILL.md', b'\xff\xfe\x00\x01')
        assert result is None


# ── 端到端集成测试（fixture） ─────────────────────────

class TestSessionSkillGitHubIntegration:
    """集成 fetcher + SessionSkillStore，验证完整流程。"""

    def test_01_fixture_success_origin_sha_state(self, tmp_path):
        """fixture 拉取成功 → origin=github、source_version 是 40 位 sha、import_state=imported。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)

        transport = _setup_success_transport(ref='main')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='main')

        record = ss.create_from_github(
            'sess-aaa', result, 'user-1')

        assert record['origin'] == 'github'
        assert record['source_version'] == FAKE_SHA
        assert SHA_RE.fullmatch(record['source_version']), \
            f'source_version 必须是 40 位 hex，实际: {record["source_version"]}'
        assert record['import_state'] == 'imported'
        assert record['name'] == 'test-github-skill'
        assert record['source_ref'] == 'owner/repo#main'
        assert record['source_sha256'] == hashlib.sha256(SKILL_MD_CONTENT).hexdigest()
        assert record['entry'] == 'SKILL.md'

    def test_02_branch_name_stored_as_resolved_sha(self, tmp_path):
        """只给分支名 → 存的是解析后的 sha，不是分支名。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)

        transport = _setup_success_transport(ref='develop')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='develop')

        record = ss.create_from_github('sess-aaa', result, 'user-1')

        # source_version 是 SHA，不是 'develop'
        assert record['source_version'] != 'develop'
        assert SHA_RE.fullmatch(record['source_version'])
        # source_ref 保留原始引用
        assert record['source_ref'] == 'owner/repo#develop'

    def test_03_fetch_failure_rejected_no_success_record(self, tmp_path):
        """拉取失败 → rejected + 结构化缺口，表里没有成功记录。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)

        transport = FakeTransport()
        transport.add('GET', '/repos/owner/nonexist/commits/HEAD', 404,
                      body={'message': 'Not Found'})
        fetcher = _fake_fetcher(transport)

        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.fetch('owner/nonexist')
        assert exc_info.value.reason == 'repo_not_found'

        # 表里没有任何记录
        items = ss.list('sess-aaa')
        assert len(items) == 0, '拉取失败不应在表里留下记录'

    def test_03b_format_noncompliant_rejected(self, tmp_path):
        """格式不合规 → 不写入成功记录。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)

        # SKILL.md 内容无效（不是 SKILL.md 也没有 name）
        transport = FakeTransport()
        transport.add('GET', '/repos/owner/repo/commits/HEAD', 200, text=FAKE_SHA)
        transport.add('GET', '/repos/owner/repo/contents/readme.md', 200,
                      body=_make_github_contents_response('readme.md', SKILL_MD_NO_NAME))
        fetcher = _fake_fetcher(transport)

        with pytest.raises(GitHubSkillFetchError) as exc_info:
            fetcher.fetch('owner/repo', subpath='readme.md')
        assert exc_info.value.reason == 'invalid_skill_format'

        items = ss.list('sess-aaa')
        assert len(items) == 0, '格式不合规不应在表里留下记录'

    def test_04_github_no_team_library_no_permission(self, tmp_path):
        """github 来源的绑定不进团队库、不授予权限。"""
        store, agent = _store_with_conversation(tmp_path)
        modules = ModuleStore(store)
        manifests = ManifestStore(store)

        modules_before = _count_table(store, 'instruction_modules')
        project_modules_before = _count_table(store, 'project_modules')
        manifest_before = manifests.get(agent['id'])
        manifest_skills_before = len(manifest_before.get('skills', []))

        # 记录授权相关表行数
        auth_counts_before = {}
        with store.connect() as db:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in tables:
                if any(k in t.lower() for k in ('authorization', 'target',
                                                  'credential', 'deploy', 'permission')):
                    auth_counts_before[t] = db.execute(
                        f'SELECT COUNT(*) FROM {t}').fetchone()[0]

        # 执行 GitHub 导入
        ss = SessionSkillStore(store)
        transport = _setup_success_transport(ref='main')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='main')
        ss.create_from_github('sess-aaa', result, 'user-1')

        # 断言团队库行数不变
        modules_after = _count_table(store, 'instruction_modules')
        project_modules_after = _count_table(store, 'project_modules')
        manifest_after = manifests.get(agent['id'])
        manifest_skills_after = len(manifest_after.get('skills', []))

        assert modules_after == modules_before
        assert project_modules_after == project_modules_before
        assert manifest_skills_after == manifest_skills_before

        # 断言授权相关表行数不变
        with store.connect() as db:
            for t, count_before in auth_counts_before.items():
                count_after = db.execute(
                    f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                assert count_after == count_before, \
                    f'{t} 行数不应变化: {count_before} -> {count_after}'

    def test_05_session_isolation_github(self, tmp_path):
        """会话隔离对 github 来源同样成立。"""
        store, agent = _store_with_conversation(tmp_path, 'sess-aaa')
        with store.connect() as db:
            db.execute('INSERT INTO agent_conversations VALUES (?,?,?)',
                       ('sess-bbb', agent['id'],
                        json.dumps({'id': 'sess-bbb', 'agent_id': agent['id'],
                                    'mode': 'do', 'messages': []})))

        ss = SessionSkillStore(store)
        transport = _setup_success_transport(ref='main')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='main')
        ss.create_from_github('sess-aaa', result, 'user-1')

        # 会话 B 看不到会话 A 的 github 绑定
        items_b = ss.list('sess-bbb')
        assert len(items_b) == 0, '会话 B 不应看到会话 A 的 github 绑定'

        # 会话 A 能看到
        items_a = ss.list('sess-aaa')
        assert len(items_a) == 1
        assert items_a[0]['origin'] == 'github'

    def test_dependencies_state(self, tmp_path):
        """有依赖 → imported + missing；无依赖 → imported + ready。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)

        # 有依赖
        transport = _setup_success_transport(ref='main')
        fetcher = _fake_fetcher(transport)
        result = fetcher.fetch('owner/repo', ref='main')
        assert result.dependencies == ['tool-alpha', 'tool-beta']
        record = ss.create_from_github('sess-aaa', result, 'user-1')
        assert record['import_state'] == 'imported'
        assert record['dependency_state'] == 'missing'

        # 无依赖
        transport2 = _setup_success_transport(
            ref='v2', content=SKILL_MD_MINIMAL)
        fetcher2 = _fake_fetcher(transport2)
        result2 = fetcher2.fetch('owner/repo', ref='v2')
        assert result2.dependencies == []
        record2 = ss.create_from_github('sess-aaa', result2, 'user-2')
        assert record2['import_state'] == 'imported'
        assert record2['dependency_state'] == 'ready'


# ── 路由测试 ──────────────────────────────────────────

class TestGitHubRoutes:
    """测试 POST /api/v4/sessions/{sid}/skills 的 GitHub 路径。"""

    @pytest.fixture
    def env(self, tmp_path):
        import subprocess
        repo = tmp_path / 'repos' / 'sample'
        repo.mkdir(parents=True)
        for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'Test'],
                     ['config', 'user.email', 'test@example.com']):
            subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
        (repo / 'greeting.txt').write_text('hello')
        subprocess.run(['git', 'add', 'greeting.txt'], cwd=repo, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True)
        data = tmp_path / 'data'
        from factory.control.store import Store
        from factory.control.service import Service
        from factory.control.providers import ProviderResult

        store = Store(data / 'control.db')

        class FakeSDK:
            def available(self):
                return [{'id': 'codex', 'installed': True, 'detail': 'test'}]
            def run(self, request, emit, cancel=None):
                return ProviderResult('done', cost_usd=0.01)

        profiles = {k: {'provider': 'codex', 'model': 'test'}
                    for k in ('planner', 'cheap', 'standard', 'strong')}
        svc = Service(store, runner=FakeSDK(), profiles=profiles)
        from factory.control.app import create_app
        app = create_app(data_dir=data, workspace_root=tmp_path / 'repos',
                         public_origin='http://testserver', service=svc,
                         webhook_secret='test-secret')
        app.state.auth.create_user('owner', 'a-long-test-password')

        # 注入假的 fetcher
        transport = _setup_success_transport(ref='main')
        app.state.github_skill_fetcher = _fake_fetcher(transport)

        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            yield client, store, svc, app

    def _login(self, client):
        resp = client.post('/api/auth/login',
                           json={'username': 'owner', 'password': 'a-long-test-password'},
                           headers={'Origin': 'http://testserver'})
        assert resp.status_code == 200
        return {'Origin': 'http://testserver', 'X-CSRF-Token': resp.json()['csrf_token']}

    def _create_conversation(self, store):
        agents = AgentStore(store)
        agent = agents.create({'name': 'RouteAgent', 'purpose': 'test'}, 'owner')
        return agents.create_conversation(agent['id'], 'do', actor_id='owner')['id']

    def test_github_post_success(self, env):
        client, store, _, _ = env
        headers = self._login(client)
        cid = self._create_conversation(store)

        resp = client.post(
            f'/api/v4/sessions/{cid}/skills',
            json={
                'origin': 'github',
                'owner_repo': 'owner/repo',
                'ref': 'main',
            },
            headers={**headers, 'Content-Type': 'application/json'})

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data['origin'] == 'github'
        assert data['name'] == 'test-github-skill'
        assert SHA_RE.fullmatch(data['source_version'])
        assert data['import_state'] == 'imported'

    def test_github_post_invalid_repo(self, env):
        client, store, _, app = env
        headers = self._login(client)
        cid = self._create_conversation(store)

        # 使用会返回错误的 fetcher
        transport = FakeTransport()
        transport.add('GET', '/repos/bad-repo/commits/HEAD', 404,
                      body={'message': 'Not Found'})
        app.state.github_skill_fetcher = _fake_fetcher(transport)

        resp = client.post(
            f'/api/v4/sessions/{cid}/skills',
            json={
                'origin': 'github',
                'owner_repo': 'bad-repo',
            },
            headers={**headers, 'Content-Type': 'application/json'})

        assert resp.status_code == 422

    def test_github_post_missing_owner_repo(self, env):
        client, store, _, _ = env
        headers = self._login(client)
        cid = self._create_conversation(store)

        resp = client.post(
            f'/api/v4/sessions/{cid}/skills',
            json={'origin': 'github'},
            headers={**headers, 'Content-Type': 'application/json'})

        assert resp.status_code == 422

    def test_zip_still_works(self, env):
        """确保 ZIP 路径不受影响。"""
        client, store, _, _ = env
        headers = self._login(client)
        cid = self._create_conversation(store)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('SKILL.md', '---\nname: zip-skill\n---\nBody\n')
        raw = buf.getvalue()

        resp = client.post(
            f'/api/v4/sessions/{cid}/skills',
            files={'file': ('skill.zip', raw, 'application/zip')},
            headers=headers)

        assert resp.status_code == 201
        assert resp.json()['origin'] == 'zip'
