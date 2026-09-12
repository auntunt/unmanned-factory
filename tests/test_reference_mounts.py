import asyncio
import copy
import io
import json
import sys
import zipfile

import pytest

from factory.control.modules import ModuleStore
from factory.control.mounts import compile_mounts, MountedRunner, ReferenceTools, create_server
from factory.control.providers import ProviderRequest, ProviderResult, ProviderError
from factory.control.source_cli import main, read_mcp_resources
from factory.control.sources import SourceStore
from factory.control.store import Conflict
from tests.test_control_app import login, project
from tests.test_workbench_app import app_env


def setup_source(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    sources = SourceStore(store)
    body = sources.put(p['id'], name='工业格式', documents=[
        {'id': 'units', 'title': '工时字段', 'uri': 'kb:hours/units', 'text': '领域规范：工时用十进制，不能截断。' + 'x' * 7000}], actor='owner')
    ref = {'id': body['id'], 'revision': body['revision']}
    modules = ModuleStore(store)
    module = modules.save(dict(name='工业转换', category='knowledge', instructions='使用格式规范', source_refs=[ref]), 'owner')
    modules.select(p['id'], [{'id': module['id'], 'version': 1}], 0, 'owner')
    run = {'project_id': p['id'], 'module_snapshot': [module]}
    return client, store, service, p, headers, sources, body, run


def test_versions_isolation_and_revocation(app_env):
    client, store, service, p, headers, sources, body, run = setup_source(app_env)
    frozen = compile_mounts(store, run)
    sources.put(p['id'], name='工业格式新版', documents=[{'id': 'units', 'title': '新版', 'text': 'new'}],
                actor='owner', sid=body['id'], expected_revision=1)
    assert compile_mounts(store, run)['digest'] == frozen['digest']
    with pytest.raises(Conflict):
        sources.put(p['id'], name='race', documents=[{'id': 'x', 'title': 'x', 'text': 'x'}],
                    actor='owner', sid=body['id'], expected_revision=1)
    with pytest.raises(ValueError, match='未授权'):
        sources.resolve('other-project', {'id': body['id'], 'revision': 1})
    with store.connect() as db, pytest.raises(Exception, match='immutable'):
        db.execute('UPDATE data_source_versions SET data=? WHERE id=?', ('{}', body['id']))
    sources.enable(p['id'], body['id'], False, 'owner')
    with pytest.raises(ValueError, match='停用'):
        compile_mounts(store, run)
    assert ReferenceTools(frozen).read(body['id'] + '/units')['source_revision'] == 1


def test_manifest_tools_page_and_reject_foreign_ids(app_env):
    _, store, _, _, _, _, body, run = setup_source(app_env)
    manifest = compile_mounts(store, run)
    tools = ReferenceTools(manifest)
    found = tools.search('工时')
    assert found['total'] == 1
    assert len(found['results'][0]['snippet']) <= 240
    first = tools.read(found['results'][0]['id'])
    second = tools.read(first['id'], first['next_offset'])
    assert first['text'] + second['text'] == manifest['documents'][0]['text']
    assert second['next_offset'] is None
    for invalid in ['../../control.db', 'other-project/doc']:
        with pytest.raises(ValueError):
            tools.read(invalid)
    with pytest.raises(ValueError): tools.read(first['id'], True)
    tampered = copy.deepcopy(manifest); tampered['documents'][0]['text'] = 'changed'
    with pytest.raises(ValueError, match='摘要'): ReferenceTools(tampered)


def test_real_sdk_server_exposes_only_reference_tools(app_env):
    pytest.importorskip('claude_agent_sdk')
    _, store, _, _, _, _, _, run = setup_source(app_env)
    events = []
    server = create_server(compile_mounts(store, run), lambda *e: events.append(e))
    assert server['name'] == 'references'
    assert server['type'] == 'sdk'
    async def protocol_roundtrip():
        import anyio
        from mcp import ClientSession
        csend, sread = anyio.create_memory_object_stream(10)
        ssend, cread = anyio.create_memory_object_stream(10)
        instance = server['instance']
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(instance.run, sread, ssend, instance.create_initialization_options())
            async with ClientSession(cread, csend) as session:
                await session.initialize()
                inventory = await session.list_tools()
                assert {t.name for t in inventory.tools} == {'search', 'read'}
                found = await session.call_tool('search', {'query': '工时'})
                assert not found.is_error
                payload = json.loads(found.content[0].text.split('\n', 1)[1])
                doc = await session.call_tool('read', {'document_id': payload['results'][0]['id']})
                assert not doc.is_error and '领域规范' in doc.content[0].text
                denied = await session.call_tool('read', {'document_id': '/etc/passwd'})
                assert denied.is_error
                bad = await session.call_tool('read', {'document_id': 'x', 'command': 'anything'})
                assert bad.is_error
            tasks.cancel_scope.cancel()
    asyncio.run(protocol_roundtrip())
    assert any(event[0] == 'source.read' for event in events)
    assert any(event[0] == 'source.read_failed' for event in events)


def test_claude_adapter_mounts_references_in_readonly_review(app_env, monkeypatch):
    sdk = pytest.importorskip('claude_agent_sdk')
    from factory.control.providers import _run_claude
    _, store, _, p, _, _, _, run = setup_source(app_env)
    manifest = compile_mounts(store, run)
    observed = []
    class ResultMessage:
        is_error = False
        result = 'done'
        session_id = 'read-only-test'
        total_cost_usd = 0
    async def query(*, prompt, options):
        assert prompt == 'review goal'
        assert set(options.mcp_servers) == {'references'}
        assert set(options.allowed_tools) == {'mcp__references__read', 'mcp__references__search'}
        assert options.tools == ['Read', 'Glob', 'Grep']
        hook = options.hooks['PreToolUse'][0].hooks[0]
        allowed = await hook({'tool_name': 'mcp__references__search', 'tool_input': {'query': '工时'}}, None, {})
        denied = await hook({'tool_name': 'mcp__evil__search', 'tool_input': {}}, None, {})
        assert allowed['hookSpecificOutput']['permissionDecision'] == 'allow'
        assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'
        observed.append(options)
        yield ResultMessage()
    monkeypatch.setattr(sdk, 'query', query)
    result = _run_claude(ProviderRequest('claude', 'test', 'review goal', p['workspace'],
                        read_only=True, reference_mount=manifest), lambda *e: None)
    assert result.text == 'done' and len(observed) == 1


def test_module_api_preserves_refs_and_rejects_mixed_revisions(app_env):
    client, store, _, p, headers, sources, body, run = setup_source(app_env)
    module = run['module_snapshot'][0]
    response = client.put('/api/v4/modules/' + module['id'], headers=headers, json={
        'name': module['name'], 'category': module['category'], 'instructions': 'updated method',
        'source_refs': module['source_refs'], 'expected_revision': 1})
    assert response.status_code == 200
    assert response.json()['source_refs'] == module['source_refs']
    assert client.get(f"/api/v4/projects/{p['id']}/sources").json()['sources'][0]['document_count'] == 1
    sources.put(p['id'], name='v2', documents=[{'id': 'a', 'title': 'a', 'text': 'a'}], actor='owner', sid=body['id'], expected_revision=1)
    second = ModuleStore(store).save(dict(name='other', category='knowledge', instructions='other',
        source_refs=[{'id': body['id'], 'revision': 2}]), 'owner')
    with pytest.raises(ValueError, match='冲突'):
        ModuleStore(store).select(p['id'], [{'id': module['id'], 'version': 1}, {'id': second['id'], 'version': 1}], 1, 'owner')


def test_real_plan_freezes_mount_and_dispatch_receives_body_outside_prompt(app_env, monkeypatch):
    client, store, service, p, headers, sources, body, configured = setup_source(app_env)
    monkeypatch.setattr(service, '_submit', lambda *args: None)
    result = client.post('/api/v2/runs', headers=headers, json={'project_id': p['id'], 'request': '修复工时字段'}).json()
    # The test fake uses Codex; compile is independent of provider selection.
    service._plan(result['id'])
    run = store.get(result['id'])
    assert run['mount_snapshot']['collections'][0]['id'] == body['id']
    assert not any(e['type'] in ('provider.started', 'usage.recorded') for e in store.events(run['id']))
    received = []
    class Runner:
        def run(self, request, emit, cancel=None):
            received.append(request)
            return ProviderResult('done')
    runner = MountedRunner(Runner(), store, run['id'])
    req = ProviderRequest('claude', 'test', 'TASK', p['workspace'], read_only=True)
    runner.run(req, lambda *e: None)
    assert received[0].prompt == 'TASK'
    assert received[0].reference_mount['documents'][0]['text'].startswith('领域规范')
    with pytest.raises(ProviderError, match='尚不支持'):
        runner.run(ProviderRequest('codex', 'test', 'TASK', p['workspace']), lambda *e: None)
    sources.enable(p['id'], body['id'], False, 'owner')
    with pytest.raises(ValueError, match='停用'): runner.run(req, lambda *e: None)
    assert len(received) == 1


def test_skill_selected_body_is_mounted_but_unselected_assets_are_not(app_env):
    _, store, _, _, _, _, _, run = setup_source(app_env)
    from factory.control.agents import AgentStore, inspect_skill
    agents = AgentStore(store)
    aid = agents.create({'name': '工程师'}, 'owner')['id']
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as z:
        z.writestr('SKILL.md', '---\nname: units\ndescription: Check units\n---\nUse references/units.md')
        z.writestr('references/units.md', '单位规则必须校验')
    meta = inspect_skill(raw.getvalue())
    sid = 'selected_skill'
    with store.connect() as db:
        db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)', (sid, aid, json.dumps(meta), raw.getvalue(), 'now'))
    run['agent_snapshot'] = {'agent_id': aid, 'version': 1, 'skill_ids': [sid]}
    mounted = compile_mounts(store, run)
    tools = ReferenceTools(mounted)
    assert tools.search('SKILL')['total'] == 1
    assert tools.read('skill/' + sid + '/references/units.md')['trust'] == 'scoped_procedure'
    run['agent_snapshot']['skill_ids'] = []
    assert not compile_mounts(store, run)['skills']
    run['agent_snapshot'] = {'agent_id': 'other', 'skill_ids': [sid]}
    with pytest.raises(PermissionError): compile_mounts(store, run)


def test_cli_import_is_atomic_and_does_not_print_document_content(app_env, tmp_path, capsys):
    _, store, _, p, _, _, _, _ = setup_source(app_env)
    path = tmp_path / 'data.json'
    path.write_text(json.dumps({'documents': [{'id': 'a', 'title': 'spec', 'text': 'PRIVATE_DOCUMENT_BODY'}]}))
    args = ['--db', str(store.path), '--project', p['id'], '--actor', 'owner', 'import-json', '--file', str(path), '--name', 'spec']
    assert main(args) == 0
    output = capsys.readouterr().out
    assert 'PRIVATE_DOCUMENT_BODY' not in output
    before = len(SourceStore(store).list(p['id']))
    path.write_text(json.dumps({'documents': [{'id': 'x', 'title': 'x', 'text': 'x'}, {'id': 'x', 'title': 'x', 'text': 'x'}]}))
    assert main(args) == 1
    assert len(SourceStore(store).list(p['id'])) == before


def test_real_mcp_stdio_resource_ingestion(tmp_path):
    pytest.importorskip('mcp.server.mcpserver')
    server = tmp_path / 'source.py'
    server.write_text('''from mcp.server.mcpserver import MCPServer
app = MCPServer("vertical-test")
@app.resource("knowledge://units")
def units() -> str:
    return "工业规范：长度单位 mm，精度 0.01。"
app.run()
''')
    documents = asyncio.run(read_mcp_resources(sys.executable, [str(server)], ['knowledge://units'], {}))
    assert documents[0]['text'] == '工业规范：长度单位 mm，精度 0.01。'
    assert documents[0]['uri'] == 'knowledge://units'


def test_portable_slot_freezes_binding_and_rejects_missing_source(app_env):
    _, store, _, p, _, sources, body, _ = setup_source(app_env)
    modules = ModuleStore(store)
    module = modules.save(dict(name='可移植转换器', category='knowledge', instructions='按格式规范转换',
                              source_slots=['format-spec']), 'owner')
    with pytest.raises(ValueError, match='format-spec'):
        modules.select(p['id'], [{'id': module['id'], 'version': 1}], 1, 'owner')
    sources.bind(p['id'], 'format-spec', {'id': body['id'], 'revision': 1}, 0, 'owner')
    modules.select(p['id'], [{'id': module['id'], 'version': 1}], 1, 'owner')
    run = {'project_id': p['id']}
    frozen = {**run, **modules.freeze(run)}
    assert compile_mounts(store, frozen)['collections'][0]['revision'] == 1
    sources.put(p['id'], name='v2', documents=[{'id': 'x', 'title': 'x', 'text': 'new'}], actor='owner', sid=body['id'], expected_revision=1)
    sources.bind(p['id'], 'format-spec', {'id': body['id'], 'revision': 2}, 1, 'owner')
    assert compile_mounts(store, frozen)['collections'][0]['revision'] == 1
    assert compile_mounts(store, {**run, **modules.freeze(run)})['collections'][0]['revision'] == 2
    with pytest.raises(Conflict):
        sources.bind(p['id'], 'format-spec', {'id': body['id'], 'revision': 1}, 1, 'owner')


def test_long_agent_guidance_appears_once_and_delivery_reaches_coding(app_env, monkeypatch):
    from tests.test_continuous_service import prepared
    from factory.control.context import context_prompt
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    instructions = 'UNIQUE_AGENT_RULE ' + 'x' * 16_000
    store.update(rid, {'agent_snapshot': {'agent_id': 'long-agent', 'version': 1, 'instructions': instructions,
                                        'acceptance': ['ACCEPTANCE_SENTINEL'], 'delivery': {'format': 'DELIVERY_SENTINEL'}}})
    svc._plan(rid)
    run = store.get(rid)
    assert run['status'] == 'queued'
    assert 'UNIQUE_AGENT_RULE' not in context_prompt(run['context'])
    captured = []
    def execute(**kwargs):
        captured.append(kwargs['plan']['tasks'][0]['prompt'])
        return {'worktree': p['workspace'], 'tasks': [{'id': 'coding', 'status': 'completed'}], 'checks': [], 'known_cost_usd': 0}
    svc.continuous_execute = execute
    monkeypatch.setattr(svc, '_independent_verify', lambda *args: None)
    svc._run(rid)
    assert len(captured) == 1
    assert captured[0].count('UNIQUE_AGENT_RULE') == 1
    assert 'ACCEPTANCE_SENTINEL' in captured[0] and 'DELIVERY_SENTINEL' in captured[0]


def test_new_source_api_requires_project_read_authorization(app_env):
    client, _, service, p, _, _, _, _ = setup_source(app_env)
    member = client.app.state.auth.create_user('reader', 'long-reader-password', role='member')
    response = client.post('/api/auth/login', json={'username': 'reader', 'password': 'long-reader-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200
    assert client.get(f"/api/v4/projects/{p['id']}/sources").status_code == 403
    service.governance.assign(member['id'], [p['id']], 'owner')
    assert client.get(f"/api/v4/projects/{p['id']}/sources").status_code == 200


def test_search_catalog_paginates_without_losing_tail_documents(app_env):
    _, store, _, p, _, sources, _, _ = setup_source(app_env)
    source = sources.put(p['id'], name='多文档', actor='owner', documents=[
        {'id': f'doc-{i:02}', 'title': f'规范 {i}', 'text': '长度单位为米'} for i in range(20)])
    module = ModuleStore(store).save(dict(name='多文档', category='knowledge', instructions='读取资料',
        source_refs=[{'id': source['id'], 'revision': 1}]), 'owner')
    tools = ReferenceTools(compile_mounts(store, {'project_id': p['id'], 'module_snapshot': [module]}))
    first = tools.search('')
    second = tools.search('', first['next_offset'])
    assert first['total'] == 20 and len(first['results']) == 12
    assert len(second['results']) == 8 and second['next_offset'] is None
    assert len({r['id'] for r in first['results'] + second['results']}) == 20
    assert tools.search('米')['total'] == 20
