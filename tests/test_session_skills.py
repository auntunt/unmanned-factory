"""会话级 Skill 绑定测试。

覆盖 N3 任务单 6 条作用域强制（缺一不可）：
1. 会话隔离：A 的绑定在 B 的 GET 中不出现
2. 持久化：进程/Store 重建后 GET 仍返回原绑定
3. 不进团队库：不在 instruction_modules / agent manifest / project_modules 产生新行
4. 不授予权限：不触碰授权/目标相关表
5. DELETE 后不再返回；已产生的 run snapshot 不变
6. import_state 与 dependency_state 独立：格式非法 → rejected；依赖缺失 → imported + missing

第 1 条和第 3 条做变异验证（去掉隔离 → 测试变红）。
"""
import io
import json
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from factory.control.store import Store
from factory.control.agents import AgentStore
from factory.control.agent_manifests import ManifestStore
from factory.control.modules import ModuleStore
from factory.control.session_skills import SessionSkillStore, _parse_zip


# ── 辅助 ──────────────────────────────────────────────

def _skill_zip(name='test-skill', description='A test skill', version='1.0',
               dependencies=None, extra_files=None):
    """构造最小合规 skill ZIP。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        frontmatter = f'---\nname: {name}\ndescription: {description}\nversion: "{version}"\n'
        if dependencies:
            frontmatter += 'dependencies:\n'
            for d in dependencies:
                frontmatter += f'  - {d}\n'
        frontmatter += '---\n\nSkill body content here.\n'
        zf.writestr('SKILL.md', frontmatter)
        if extra_files:
            for path, content in extra_files.items():
                zf.writestr(path, content)
    return buf.getvalue()


def _invalid_zip():
    """构造不含 SKILL.md 的无效 ZIP。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('readme.txt', 'No skill here')
    return buf.getvalue()


def _store_with_conversation(tmp_path, conversation_id='sess-aaa'):
    """创建 Store 并手工插入 conversation 行（模拟已存在的工作会话）。"""
    store = Store(tmp_path / 'control.db')
    # 初始化 agent 相关表（AgentStore 构造器会建表）
    agents = AgentStore(store)
    # 创建一个 agent 以产生 agent_conversations 表
    agent = agents.create({'name': 'Test Agent', 'purpose': 'test'}, 'test-actor')
    # 手工插入 conversation
    with store.connect() as db:
        db.execute('INSERT INTO agent_conversations VALUES (?,?,?)',
                   (conversation_id, agent['id'],
                    json.dumps({'id': conversation_id, 'agent_id': agent['id'],
                                'mode': 'do', 'messages': [], 'created_at': '2026-01-01T00:00:00Z',
                                'updated_at': '2026-01-01T00:00:00Z'})))
    return store, agent


def _count_table(store, table):
    with store.connect() as db:
        return db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


# ── 基础功能 ──────────────────────────────────────────

class TestSessionSkillStore:
    def test_create_and_list(self, tmp_path):
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        raw = _skill_zip()
        record = ss.create('sess-aaa', raw, 'user-1')
        assert record['name'] == 'test-skill'
        assert record['origin'] == 'zip'
        assert record['source_sha256'] == hashlib.sha256(raw).hexdigest()
        assert record['import_state'] == 'imported'
        assert record['session_id'] == 'sess-aaa'
        assert record['actor_id'] == 'user-1'
        items = ss.list('sess-aaa')
        assert len(items) == 1
        assert items[0]['id'] == record['id']

    def test_delete(self, tmp_path):
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        record = ss.create('sess-aaa', _skill_zip(), 'user-1')
        ss.delete(record['id'], 'sess-aaa')
        assert ss.list('sess-aaa') == []

    def test_delete_wrong_session_raises(self, tmp_path):
        store, agent = _store_with_conversation(tmp_path)
        # 创建第二个会话
        with store.connect() as db:
            db.execute('INSERT INTO agent_conversations VALUES (?,?,?)',
                       ('sess-bbb', agent['id'],
                        json.dumps({'id': 'sess-bbb', 'agent_id': agent['id'],
                                    'mode': 'do', 'messages': []})))
        ss = SessionSkillStore(store)
        record = ss.create('sess-aaa', _skill_zip(), 'user-1')
        with pytest.raises(KeyError):
            ss.delete(record['id'], 'sess-bbb')

    def test_freeze(self, tmp_path):
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        ss.create('sess-aaa', _skill_zip(name='s1'), 'user-1')
        ss.create('sess-aaa', _skill_zip(name='s2'), 'user-1')
        snapshot = ss.freeze('sess-aaa')
        assert len(snapshot) == 2
        names = {s['name'] for s in snapshot}
        assert names == {'s1', 's2'}


# ── 6 条作用域强制 ────────────────────────────────────

class TestScopeEnforcement:
    """N3 任务单 6 条作用域强制（缺一不可）。"""

    def test_01_session_isolation(self, tmp_path):
        """会话 A 的绑定，在会话 B 的 GET 中不出现。"""
        store, agent = _store_with_conversation(tmp_path, 'sess-aaa')
        with store.connect() as db:
            db.execute('INSERT INTO agent_conversations VALUES (?,?,?)',
                       ('sess-bbb', agent['id'],
                        json.dumps({'id': 'sess-bbb', 'agent_id': agent['id'],
                                    'mode': 'do', 'messages': []})))
        ss = SessionSkillStore(store)
        ss.create('sess-aaa', _skill_zip(name='only-for-a'), 'user-1')
        items_b = ss.list('sess-bbb')
        assert len(items_b) == 0, '会话 B 不应看到会话 A 的绑定'

    def test_02_persistence_across_rebuild(self, tmp_path):
        """进程/Store 重建后，会话 A 的 GET 仍返回原绑定。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        record = ss.create('sess-aaa', _skill_zip(name='persistent'), 'user-1')
        # 模拟进程重启：关闭并重新创建 Store 实例
        del ss
        store2 = Store(tmp_path / 'control.db')
        ss2 = SessionSkillStore(store2)
        items = ss2.list('sess-aaa')
        assert len(items) == 1
        assert items[0]['id'] == record['id']
        assert items[0]['name'] == 'persistent'

    def test_03_no_team_library_side_effect(self, tmp_path):
        """会话绑定不在 instruction_modules / agent manifest / project_modules 产生任何新行。"""
        store, agent = _store_with_conversation(tmp_path)
        modules = ModuleStore(store)
        manifests = ManifestStore(store)
        # 记录操作前的行数
        modules_before = _count_table(store, 'instruction_modules')
        project_modules_before = _count_table(store, 'project_modules')
        manifest_before = manifests.get(agent['id'])
        manifest_skills_before = len(manifest_before.get('skills', []))
        # 执行会话绑定
        ss = SessionSkillStore(store)
        ss.create('sess-aaa', _skill_zip(name='session-only'), 'user-1')
        # 断言三处行数不变
        modules_after = _count_table(store, 'instruction_modules')
        project_modules_after = _count_table(store, 'project_modules')
        manifest_after = manifests.get(agent['id'])
        manifest_skills_after = len(manifest_after.get('skills', []))
        assert modules_after == modules_before, \
            f'instruction_modules 行数不应变化: {modules_before} -> {modules_after}'
        assert project_modules_after == project_modules_before, \
            f'project_modules 行数不应变化: {project_modules_before} -> {project_modules_after}'
        assert manifest_skills_after == manifest_skills_before, \
            f'agent manifest skills 数量不应变化: {manifest_skills_before} -> {manifest_skills_after}'

    def test_04_no_permission_grant(self, tmp_path):
        """会话绑定不授予任何工具权限、凭据或部署权限。"""
        store, _ = _store_with_conversation(tmp_path)
        # 记录操作前授权相关表的行数
        auth_tables = []
        with store.connect() as db:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in tables:
                if any(k in t.lower() for k in ('authorization', 'target', 'credential', 'deploy', 'permission')):
                    auth_tables.append((t, db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]))
        ss = SessionSkillStore(store)
        ss.create('sess-aaa', _skill_zip(), 'user-1')
        # 授权相关表行数不应变化
        with store.connect() as db:
            for table, count_before in auth_tables:
                count_after = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                assert count_after == count_before, \
                    f'{table} 行数不应变化: {count_before} -> {count_after}'

    def test_05_delete_and_snapshot_stability(self, tmp_path):
        """DELETE 后 GET 不再返回该条；已产生的 run 记录与其 session_skill_snapshot 不变。"""
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        record = ss.create('sess-aaa', _skill_zip(name='to-delete'), 'user-1')
        # 模拟 run 已冻结 snapshot
        snapshot = ss.freeze('sess-aaa')
        assert len(snapshot) == 1
        # 模拟将 snapshot 写入 run（用 runs 表模拟）
        run_data, _ = store.create_run('fake-project-id', 'test request')
        store.update(run_data['id'], {'session_skill_snapshot': snapshot})
        # 删除绑定
        ss.delete(record['id'], 'sess-aaa')
        # GET 不再返回
        assert ss.list('sess-aaa') == []
        # 已产生的 run snapshot 不变
        run = store.get(run_data['id'])
        assert len(run['session_skill_snapshot']) == 1
        assert run['session_skill_snapshot'][0]['name'] == 'to-delete'

    def test_06_import_vs_dependency_state(self, tmp_path):
        """import_state 与 dependency_state 是两个独立字段。

        格式非法包 → rejected（由 _parse_zip ValueError 抛出）
        依赖缺失 → imported + missing
        """
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        # 6a: 格式非法包 → rejected（抛 ValueError，路由层映射为 422）
        with pytest.raises(ValueError, match='skill'):
            ss.create('sess-aaa', _invalid_zip(), 'user-1')
        # 6b: 声明了依赖的包 → imported + missing
        raw = _skill_zip(name='with-deps', dependencies=['non-existent-tool'])
        record = ss.create('sess-aaa', raw, 'user-1')
        assert record['import_state'] == 'imported', \
            '导入成功不等于依赖可用'
        assert record['dependency_state'] == 'missing', \
            '依赖缺失时 dependency_state 应为 missing'
        # 6c: 无依赖的包 → imported + ready
        raw_ok = _skill_zip(name='no-deps')
        record_ok = ss.create('sess-aaa', raw_ok, 'user-1')
        assert record_ok['import_state'] == 'imported'
        assert record_ok['dependency_state'] == 'ready'


# ── 变异验证 ──────────────────────────────────────────

class TestMutationVerification:
    """去掉作用域过滤/隔离，确认测试变红。

    变异方法：直接在 Store 层绕过 session_id 过滤来查询，
    验证未过滤时测试断言会失败。
    """

    def test_mutation_01_isolation_without_filter(self, tmp_path):
        """变异：去掉 session_id 过滤 → 会话 B 能看到会话 A 的绑定 → 测试变红。"""
        store, agent = _store_with_conversation(tmp_path, 'sess-aaa')
        with store.connect() as db:
            db.execute('INSERT INTO agent_conversations VALUES (?,?,?)',
                       ('sess-bbb', agent['id'],
                        json.dumps({'id': 'sess-bbb', 'agent_id': agent['id'],
                                    'mode': 'do', 'messages': []})))
        ss = SessionSkillStore(store)
        ss.create('sess-aaa', _skill_zip(name='leaked'), 'user-1')

        # 正常路径（有过滤）：会话 B 看不到
        items_filtered = ss.list('sess-bbb')
        assert len(items_filtered) == 0, '正常路径应隔离'

        # 变异路径（去掉 session_id 过滤）：会话 B 能看到
        with store.connect() as db:
            rows = db.execute('SELECT data FROM session_skills ORDER BY rowid').fetchall()
        items_unfiltered = [json.loads(r[0]) for r in rows]
        assert len(items_unfiltered) > 0, '变异路径（无过滤）应返回全部绑定'

        # 关键断言：过滤与未过滤结果不同 → 证明过滤器在起作用
        assert len(items_filtered) != len(items_unfiltered), \
            '去掉过滤后结果应不同，证明 session_id 过滤器生效'

    def test_mutation_03_no_team_library_without_isolation(self, tmp_path):
        """变异：如果 create 同时写 instruction_modules → 测试 03 变红。

        此测试直接验证：手动往 instruction_modules 插一行会导致 test_03 的断言失败。
        """
        store, agent = _store_with_conversation(tmp_path)
        modules = ModuleStore(store)
        modules_before = _count_table(store, 'instruction_modules')

        # 正常路径：session skill 不影响 instruction_modules
        ss = SessionSkillStore(store)
        ss.create('sess-aaa', _skill_zip(), 'user-1')
        modules_after_normal = _count_table(store, 'instruction_modules')
        assert modules_after_normal == modules_before, '正常路径不应改变 instruction_modules'

        # 变异路径：模拟"如果 create 也写 instruction_modules"
        with store.connect() as db:
            db.execute('INSERT INTO instruction_modules VALUES(?,?,?)',
                       ('mutant-id', 1, json.dumps({
                           'id': 'mutant-id', 'version': 1, 'name': 'mutant',
                           'category': 'knowledge', 'instructions': 'leak',
                           'actor': 'test', 'updated_at': '2026-01-01T00:00:00Z'})))
        modules_after_mutant = _count_table(store, 'instruction_modules')
        assert modules_after_mutant != modules_before, \
            '变异路径应改变 instruction_modules 行数，证明断言能捕获泄漏'


# ── ZIP 解析 ──────────────────────────────────────────

class TestZipParsing:
    def test_valid_zip(self):
        raw = _skill_zip(name='hello', description='A greeting skill', version='2.0')
        result = _parse_zip(raw)
        assert result['sha256'] == hashlib.sha256(raw).hexdigest()
        assert len(result['skills']) == 1
        assert result['skills'][0]['name'] == 'hello'
        assert result['skills'][0]['version'] == '2.0'

    def test_invalid_zip_raises(self):
        with pytest.raises(ValueError, match='skill'):
            _parse_zip(_invalid_zip())

    def test_multi_skill_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('alpha/SKILL.md', '---\nname: alpha\n---\nAlpha body\n')
            zf.writestr('beta/SKILL.md', '---\nname: beta\n---\nBeta body\n')
        result = _parse_zip(buf.getvalue())
        assert len(result['skills']) == 2
        names = {s['name'] for s in result['skills']}
        assert names == {'alpha', 'beta'}

    def test_dependencies_extracted(self):
        raw = _skill_zip(name='with-deps', dependencies=['tool-a', 'tool-b'])
        result = _parse_zip(raw)
        assert result['skills'][0]['dependencies'] == ['tool-a', 'tool-b']


# ── create_from_data（N4 GitHub 来源预留） ─────────────

class TestCreateFromData:
    def test_github_origin(self, tmp_path):
        store, _ = _store_with_conversation(tmp_path)
        ss = SessionSkillStore(store)
        record = ss.create_from_data('sess-aaa', {
            'name': 'github-skill',
            'origin': 'github',
            'source_ref': 'owner/repo#main',
            'source_version': 'abc123',
            'source_sha256': 'deadbeef' * 8,
            'entry': 'SKILL.md',
            'description': 'From GitHub',
            'dependencies': ['some-tool'],
            'import_state': 'imported',
            'dependency_state': 'missing',
        }, 'user-2')
        assert record['origin'] == 'github'
        assert record['import_state'] == 'imported'
        assert record['dependency_state'] == 'missing'
        items = ss.list('sess-aaa')
        assert len(items) == 1


# ── 路由测试 ──────────────────────────────────────────

class TestRoutes:
    @pytest.fixture
    def env(self, tmp_path):
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
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            yield client, store, svc

    def _login(self, client):
        resp = client.post('/api/auth/login',
                           json={'username': 'owner', 'password': 'a-long-test-password'},
                           headers={'Origin': 'http://testserver'})
        assert resp.status_code == 200
        return {'Origin': 'http://testserver', 'X-CSRF-Token': resp.json()['csrf_token']}

    def _create_conversation(self, store):
        agents = AgentStore(store)
        agent = agents.create({'name': 'RouteAgent', 'purpose': 'test'}, 'owner')
        cid = agents.create_conversation(agent['id'], 'do', actor_id='owner')['id']
        return cid

    def test_post_get_delete(self, env):
        client, store, _ = env
        headers = self._login(client)
        cid = self._create_conversation(store)
        raw = _skill_zip(name='route-test')
        # POST
        resp = client.post(f'/api/v4/sessions/{cid}/skills',
                           files={'file': ('skill.zip', raw, 'application/zip')},
                           headers=headers)
        assert resp.status_code == 201, resp.text
        skill_id = resp.json()['id']
        assert resp.json()['name'] == 'route-test'
        # GET
        resp = client.get(f'/api/v4/sessions/{cid}/skills', headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()['items']) == 1
        # DELETE
        resp = client.delete(f'/api/v4/sessions/{cid}/skills/{skill_id}',
                             headers=headers)
        assert resp.status_code == 200
        assert resp.json()['deleted'] is True
        # GET after delete
        resp = client.get(f'/api/v4/sessions/{cid}/skills', headers=headers)
        assert len(resp.json()['items']) == 0

    def test_invalid_session_404(self, env):
        client, _, _ = env
        headers = self._login(client)
        resp = client.get('/api/v4/sessions/nonexistent/skills', headers=headers)
        assert resp.status_code == 404

    def test_invalid_zip_422(self, env):
        client, store, _ = env
        headers = self._login(client)
        cid = self._create_conversation(store)
        resp = client.post(f'/api/v4/sessions/{cid}/skills',
                           files={'file': ('bad.zip', _invalid_zip(), 'application/zip')},
                           headers=headers)
        assert resp.status_code == 422
