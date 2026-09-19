"""聊天里直接调用本角色已挂靠的能力包。

目标行为：用户在同一个职能体里说「按规范整理并生成文件」，模型整理出结构化内容后
**直接调用**已发布并挂靠的 CLI 工具，拿回真实产物下载链接——不再让用户去理解 JSON
契约、先下载中间 TXT 再重新上传。

这里刻意走完整链路：服务端生成会话绑定 → **JSON 序列化跨进程** → 在 worker 侧重建
→ 真实 MCP 客户端往返 → PackStore 权限闸门 → `run_tool` 真实隔离执行 → 真实 HTTP
下载。不 stub `service.runner.run`，也不直接调内部方法冒充链路。
"""
import io
import json

import anyio
import pytest
from mcp import ClientSession

from factory.control import conversation_tools as ct
from tests.test_control_app import app_env, login, project  # noqa: F401
from tests.test_capability_packs import published_pack, key, TOOL_SOURCE  # noqa: F401


def _conversation(client, headers, aid):
    created = client.post(f'/api/v4/agents/{aid}/conversations',
                          json={'mode': 'do'}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()['id']


def _worker_tools(store, cid, actor_id, *, role='admin'):
    """服务端生成绑定 → JSON 往返 → worker 侧重建。这一步就是跨进程边界。"""
    binding = ct.binding_for(store, cid, actor_id, actor_role=role)
    crossed = json.loads(json.dumps(binding))
    assert crossed == binding, '绑定必须可序列化且无损'
    return ct.ConversationTools.from_binding(crossed)


def _call(tools, name, arguments):
    """真实 MCP 往返：和模型看到的是同一套注册与调用路径。"""
    server = ct.create_server(tools, lambda *e: None)
    instance = server['instance']
    box = {}

    async def roundtrip():
        csend, sread = anyio.create_memory_object_stream(10)
        ssend, cread = anyio.create_memory_object_stream(10)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(instance.run, sread, ssend, instance.create_initialization_options())
            async with ClientSession(cread, csend) as session:
                await session.initialize()
                box['names'] = {t.name for t in (await session.list_tools()).tools}
                result = await session.call_tool(name, arguments)
                box['is_error'] = bool(result.is_error)
                box['text'] = ''.join(c.text for c in result.content if getattr(c, 'text', None))
            tasks.cancel_scope.cancel()

    anyio.run(roundtrip)
    return box


def _actor_id(store, cid):
    """会话上记录的就是服务端认定的发起者——和 binding_for 用的是同一个值。"""
    from factory.control.agents import AgentStore
    return AgentStore(store).conversation(cid)['actor_id']


@pytest.fixture
def bound_chat(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': '会议助手', 'purpose': '按规范整理会议材料'},
                      headers=headers).json()['id']
    bound = client.post('/api/v4/capability-packs/bindings', headers=headers,
                        json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    assert bound.status_code == 201, bound.text
    cid = _conversation(client, headers, aid)
    return client, store, headers, pack, version, aid, cid


# ---- 正向：整理→调用→真实文件 ------------------------------------------

def test_chat_lists_and_runs_the_attached_pack_and_the_file_is_real(bound_chat):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))

    listed = _call(tools, 'attached_tools', {})
    assert {'attached_tools', 'run_attached_tool'} <= listed['names']
    offered = json.loads(listed['text'])
    assert len(offered) == 1, offered
    entry = offered[0]
    # 真实名称、版本、操作键与限制都来自服务端，不是模型猜的。
    assert entry['pack_id'] == pack['id'] and entry['version'] == version['version']
    assert entry['tool_name'] and entry['purpose']
    assert entry['timeout_seconds'] and entry['max_input_bytes']

    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    ran = _call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': '会议清单.csv'})
    assert not ran['is_error'], ran['text']
    result = json.loads(ran['text'])
    assert result['status'] == 'succeeded', result
    assert result['version'] == version['version']
    assert result['inputs'] and result['inputs'][0]['sha256']
    assert result['outputs'], result

    # 下载的是真实保存的产物，走真实 HTTP 授权端点。
    output = result['outputs'][0]
    download = client.get(output['download_path'], headers=headers)
    assert download.status_code == 200, download.text
    produced = download.content.decode('utf-8')
    expected = (TOOL_SOURCE / 'fixtures/expected_quote.xml').read_text(encoding='utf-8')
    items = [line for line in expected.splitlines() if line.strip().startswith('<Item')]
    assert [line for line in produced.splitlines() if line.strip().startswith('<Item')] == items


def test_revision_keeps_the_old_file_and_says_which_input_made_which(bound_chat):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')

    first = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': 'v1.csv'})['text'])
    # 合法的修订：补一条完整行（与表头列数一致），模拟人工反馈后重新生成。
    revised = source.rstrip('\n') + '\nB-011,模板拆除,m2,240,12.40\n'
    second = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': revised, 'filename': 'v2.csv'})['text'])

    assert first['task_id'] != second['task_id']
    assert first['inputs'][0]['sha256'] != second['inputs'][0]['sha256']
    # 旧版本仍可下载：修订不覆盖已交付的文件。
    for item in (first, second):
        assert client.get(item['outputs'][0]['download_path'], headers=headers).status_code == 200


def test_identical_content_replays_instead_of_running_the_tool_twice(bound_chat, monkeypatch):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    args = {'pack_id': pack['id'], 'content': source, 'filename': 'same.csv'}

    first = json.loads(_call(tools, 'run_attached_tool', args)['text'])
    assert first['status'] == 'succeeded' and not first['replayed']

    calls = []
    import factory.control.conversation_pack_tools as cpt
    real = cpt.run_tool
    monkeypatch.setattr(cpt, 'run_tool', lambda *a, **k: calls.append(1) or real(*a, **k))
    second = json.loads(_call(tools, 'run_attached_tool', args)['text'])
    assert second['task_id'] == first['task_id'] and second['replayed'] is True
    assert calls == [], '同样的内容不得把工具再跑一遍'


# ---- 反向：未挂靠 / 无关用户 / 失效关系 ----------------------------------

def test_unattached_pack_is_refused_and_never_reaches_the_executor(bound_chat, monkeypatch):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    calls = []
    import factory.control.conversation_pack_tools as cpt
    monkeypatch.setattr(cpt, 'run_tool', lambda *a, **k: calls.append(1))

    ran = _call(tools, 'run_attached_tool', {
        'pack_id': 'pack-that-was-never-attached', 'content': 'x'})
    assert ran['is_error'], ran['text']
    assert json.loads(ran['text'])['refused'] == 'not_attached'
    assert calls == [], '未挂靠的包不得进入执行层'


def test_role_without_any_binding_offers_nothing_and_runs_nothing(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    aid = client.post('/api/v4/agents', json={'name': '空助手', 'purpose': '没有挂靠'},
                      headers=headers).json()['id']
    cid = _conversation(client, headers, aid)
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    calls = []
    import factory.control.conversation_pack_tools as cpt
    monkeypatch.setattr(cpt, 'run_tool', lambda *a, **k: calls.append(1))

    assert json.loads(_call(tools, 'attached_tools', {})['text']) == []
    ran = _call(tools, 'run_attached_tool', {
        'pack_id': 'anything', 'content': 'x'})
    assert ran['is_error'] and json.loads(ran['text'])['refused'] == 'not_attached'
    assert calls == []


def test_unbinding_stops_further_calls_even_with_the_known_pack_id(bound_chat, monkeypatch):
    """失效的挂靠关系不能被之前见过的标识绕过。"""
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))

    removed = client.delete(f"/api/v4/capability-packs/bindings/{aid}/{pack['id']}", headers=headers)
    assert removed.status_code == 200, removed.text

    calls = []
    import factory.control.conversation_pack_tools as cpt
    monkeypatch.setattr(cpt, 'run_tool', lambda *a, **k: calls.append(1))
    ran = _call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': 'x'})
    assert ran['is_error'], ran['text']
    assert json.loads(ran['text'])['refused'] == 'not_attached'
    assert calls == []
    assert json.loads(_call(tools, 'attached_tools', {})['text']) == []


def test_a_tool_that_really_fails_comes_back_as_a_real_failure(bound_chat):
    """工具跑了但拒绝了输入：必须原样回报失败与错误码。

    这条挡住「模型说生成成功、其实没有产物」这类叙述性成功——调用结果里没有
    outputs，status 不是 succeeded，模型拿到的就是失败。
    """
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))

    ran = _call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': '这不是清单 CSV，没有表头也没有列\n', 'filename': 'bad.csv'})
    # 工具真的跑了并拒绝 —— 不是 MCP 层的调用错误。
    assert not ran['is_error'], ran['text']
    result = json.loads(ran['text'])
    assert result['status'] != 'succeeded', result
    assert result['outputs'] == [], result
    assert result['error_code'] or result['error'], result
    # 版本与输入指纹仍然留证，便于查是哪次输入失败的。
    assert result['version'] == version['version'] and result['inputs'][0]['sha256']
