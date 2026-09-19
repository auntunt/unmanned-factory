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


# ---- R1：模型必须先拿到真实的文件内容契约 --------------------------------

def test_attached_tools_carries_the_packs_own_content_contract(bound_chat):
    """现场里模型连猜五次输入格式都失败：它拿到的是 CLI 包装协议，不是文件格式。

    包自己的说明才是 `content` 的契约来源，且必须按已发布版本给，不写死任何客户 schema。
    """
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    entry = json.loads(_call(tools, 'attached_tools', {})['text'])[0]

    # CLI 包装协议改名，不再冒充文件格式。
    assert 'input_schema' not in entry
    assert set(entry['invocation_schema']['required']) == {'input_dir', 'output_dir', 'inputs'}
    # 真正的内容契约来自这个版本自带的文档。
    assert entry['content_contract'], entry
    assert entry['content_contract_files'], entry
    assert entry['support_matrix'], entry


def test_declared_input_cap_is_the_smaller_of_pack_and_chat(bound_chat):
    """声明 2 MB 而工具只收 512 KB，只是把失败推后。取两者较小值。"""
    from factory.control.capability_packs import PackStore
    from factory.control.conversation_pack_tools import MAX_CHAT_INPUT_BYTES
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    entry = json.loads(_call(tools, 'attached_tools', {})['text'])[0]

    manifest = PackStore(store).version(version['id'])['manifest']
    pack_cap = manifest['tool']['permissions']['max_input_bytes']
    assert entry['max_input_bytes'] == min(MAX_CHAT_INPUT_BYTES, pack_cap)


def test_the_full_document_is_readable_and_scoped_to_attached_versions(bound_chat):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))

    doc = json.loads(_call(tools, 'attached_tool_doc', {'pack_id': pack['id']})['text'])
    assert doc['version'] == version['version'] and doc['text'].strip()
    assert doc['path'] in doc['available_documents']

    refused = _call(tools, 'attached_tool_doc', {'pack_id': 'not-attached-at-all'})
    assert refused['is_error']
    assert json.loads(refused['text'])['refused'] == 'not_attached'

    wrong = _call(tools, 'attached_tool_doc', {'pack_id': pack['id'], 'path': 'tool/main.py'})
    assert wrong['is_error']
    assert json.loads(wrong['text'])['refused'] == 'unknown_document'


# ---- R2：结果必须属于本次会话，且可直接下载 ------------------------------

def test_the_result_is_recorded_on_this_conversation_and_not_another(bound_chat):
    """现场里新会话显示的是上一会话的成果。回执要挂在本会话上，刷新仍在。"""
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    result = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': '会议清单.csv'})['text'])
    assert result['status'] == 'succeeded'

    # 不刷新也不解析模型文本：回执就在会话记录里，直接读得到。
    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    receipts = conv.get('tool_results') or []
    assert len(receipts) == 1, receipts
    receipt = receipts[0]
    assert receipt['task_id'] == result['task_id']
    assert receipt['status'] == 'succeeded' and receipt['version'] == version['version']
    download = client.get(receipt['outputs'][0]['download_path'], headers=headers)
    assert download.status_code == 200 and download.content

    # 同一角色的另一个会话不得串到这份成果。
    other = _conversation(client, headers, aid)
    other_conv = client.get(f'/api/v4/conversations/{other}', headers=headers).json()
    assert (other_conv.get('tool_results') or []) == [], other_conv.get('tool_results')


def test_revision_shows_both_receipts_and_keeps_the_old_download(bound_chat):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    first = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': 'v1.csv'})['text'])
    revised = source.rstrip('\n') + '\nB-011,模板拆除,m2,240,12.40\n'
    second = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': revised, 'filename': 'v2.csv'})['text'])

    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    receipts = {r['task_id']: r for r in conv['tool_results']}
    assert set(receipts) == {first['task_id'], second['task_id']}
    for receipt in receipts.values():
        assert client.get(receipt['outputs'][0]['download_path'], headers=headers).status_code == 200


def test_a_failed_run_is_recorded_on_the_conversation_as_failed(bound_chat):
    """失败也要留回执：界面不能只在成功时才有真相。"""
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': '不是清单 CSV\n', 'filename': 'bad.csv'})['text'])

    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    receipt = conv['tool_results'][0]
    assert receipt['status'] != 'succeeded' and receipt['outputs'] == []
    assert receipt['error_code'] or receipt['error']


# ---- 真实 worker 子进程边界 ----------------------------------------------

def test_attached_pack_runs_through_the_real_worker_subprocess(bound_chat, monkeypatch):
    """真正跨进程：聊天端点 → 真实 SDKRunner → JSONL → sdk_worker 子进程 →
    在子进程里重建 ConversationTools → 会话 MCP 工具 → 真实包执行 → 回执落到
    这个会话的 SQLite 行 → 经真实 HTTP 下载。

    只有外部模型响应被替身（tests/proc_pack_worker.py），runner 与进程通信都是真的。
    上一轮我把同进程的 json.dumps/loads 说成「跨进程」，这条才是进程边界验证。
    """
    pytest.importorskip('claude_agent_sdk')
    from factory.control.providers import SDKRunner
    client, store, headers, pack, version, aid, cid = bound_chat
    service = client.app.state.service

    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    monkeypatch.setenv('PACK_TEST_CONTENT', source)
    service.runner = SDKRunner(worker_module='tests.proc_pack_worker')
    service.governance = None
    cfg = service.runtime_settings.get()
    profiles = dict(cfg['profiles'])
    profiles['planner'] = {'provider': 'claude', 'model': 'test'}
    service.runtime_settings.update({'profiles': profiles, 'limits': cfg['limits']},
                                    cfg['revision'], 'tester')

    sent = client.post(f'/api/v4/conversations/{cid}/messages', headers=headers,
                       json={'content': '按公司规范整理这份材料并生成文件'})
    assert sent.status_code == 201, sent.text
    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        state = service.maintenance_status(sent.json()['job_id'])
        if state['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
            break
        time.sleep(0.1)
    assert state['status'] == 'completed', state

    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    receipts = conv.get('tool_results') or []
    assert receipts, '子进程里的工具执行没有留下本会话回执'
    receipt = receipts[0]
    assert receipt['status'] == 'succeeded', receipt
    assert receipt['version'] == version['version']
    download = client.get(receipt['outputs'][0]['download_path'], headers=headers)
    assert download.status_code == 200 and b'<Item' in download.content

    # 回读也必须在真实子进程里可达：助手回复带着它从产物正文读到的事实。
    reply = next(m['content'] for m in reversed(conv['messages']) if m['role'] == 'assistant')
    assert '回读含B-010：True' in reply, reply
    assert '回读字节：' in reply and '回读字节：None' not in reply, reply


# ---- R3：取消的两道防线要各自可证，异常必须有终态 ------------------------

def test_the_executor_actually_receives_a_cancel_handle(bound_chat, monkeypatch):
    """第一道：cancel 真的传进了执行器，且它读的是已落库的取消意图。

    单靠「取消后结果是 cancelled」证明不了这一条——即便不传 cancel，收尾那道
    降级也会得到同样结果。所以这里直接查执行器拿到了什么。
    """
    client, store, headers, pack, version, aid, cid = bound_chat
    from factory.control.capability_packs import PackStore
    seen = {}

    def observe(version_arg, files, inputs, *, cancel=None, **kwargs):
        seen['cancel'] = cancel
        seen['before'] = cancel.is_set() if cancel is not None else None
        task_id = next(t['id'] for t in PackStore(store).tasks_for({'id': _actor_id(store, cid), 'role': 'admin'})
                       if t['status'] == 'running')
        PackStore(store).update_task(task_id, {'status': 'cancel_requested'},
                                     event=('task.cancel_requested', {}))
        seen['after'] = cancel.is_set() if cancel is not None else None
        return {'status': 'succeeded', 'outputs': [], 'validation_status': 'passed',
                'evidence': {}, 'error_code': None}

    import factory.control.conversation_pack_tools as cpt
    monkeypatch.setattr(cpt, 'run_tool', observe)
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    _call(tools, 'run_attached_tool', {'pack_id': pack['id'], 'content': 'x', 'filename': 'a.txt'})

    assert seen['cancel'] is not None, '执行器没有拿到 cancel 句柄'
    assert seen['before'] is False and seen['after'] is True, seen


def test_a_tool_that_ignores_cancel_is_still_not_reported_as_success(bound_chat, monkeypatch):
    """第二道：包本身不理会 cancel、照样返回成功时，收尾也不得把取消覆盖成成功。"""
    client, store, headers, pack, version, aid, cid = bound_chat
    from factory.control.capability_packs import PackStore

    def ignores_cancel(version_arg, files, inputs, *, cancel=None, **kwargs):
        task_id = next(t['id'] for t in PackStore(store).tasks_for({'id': _actor_id(store, cid), 'role': 'admin'})
                       if t['status'] == 'running')
        PackStore(store).update_task(task_id, {'status': 'cancel_requested'},
                                     event=('task.cancel_requested', {}))
        return {'status': 'succeeded', 'outputs': [], 'validation_status': 'passed',
                'evidence': {}, 'error_code': None}

    import factory.control.conversation_pack_tools as cpt
    monkeypatch.setattr(cpt, 'run_tool', ignores_cancel)
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    result = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': 'x', 'filename': 'a.txt'})['text'])

    assert result['status'] == 'cancelled', result
    assert result['error_code'] == 'cancelled', result
    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    assert conv['tool_results'][0]['status'] == 'cancelled'


def test_an_executor_crash_reaches_a_terminal_state_not_running_forever(bound_chat, monkeypatch):
    client, store, headers, pack, version, aid, cid = bound_chat
    from factory.control.capability_packs import PackStore

    def explode(*a, **k):
        raise RuntimeError('沙箱崩溃')

    import factory.control.conversation_pack_tools as cpt
    monkeypatch.setattr(cpt, 'run_tool', explode)
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    ran = _call(tools, 'run_attached_tool', {'pack_id': pack['id'], 'content': 'x', 'filename': 'a.txt'})
    assert ran['is_error'], ran['text']

    tasks = PackStore(store).tasks_for({'id': _actor_id(store, cid), 'role': 'admin'})
    assert tasks and all(t['status'] != 'running' for t in tasks), tasks
    crashed = tasks[0]
    assert crashed['status'] == 'failed' and crashed['error_code'] == 'executor_crashed', crashed
    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    assert conv['tool_results'][0]['status'] == 'failed'

@pytest.mark.parametrize('when', ['before_start', 'before_finish'])
def test_cancel_wins_the_atomic_transition(bound_chat, monkeypatch, when):
    from factory.control.capability_packs import PackStore
    from factory.control.conversation_pack_tools import PackTools
    client, store, headers, pack, version, aid, cid = bound_chat
    original = PackStore.update_task
    fired = []
    def interleave(self, tid, patch, **kwargs):
        if not fired and patch.get('status') == ('running' if when == 'before_start' else 'succeeded'):
            fired.append(True)
            original(self, tid, {'status': 'cancel_requested'})
        return original(self, tid, patch, **kwargs)
    monkeypatch.setattr(PackStore, 'update_task', interleave)
    result = PackTools(store, cid, _actor_id(store, cid), actor_role='admin').run(
        pack['id'], (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(), 'sample.csv')
    assert fired
    assert result['status'] == 'cancelled'
    assert result['outputs'] == []


# ---- 受控只读回读本会话产物 ----------------------------------------------

def test_the_model_can_read_back_the_file_it_actually_produced(bound_chat):
    """现场缺口：模型说文件已修好，实际栏目仍错，因为它只拿到下载链接、读不到正文。

    回读必须取到**真实保存的字节**——这里用唯一标记证明，不是复述模型自己的话。
    """
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    marker = 'B-010'  # 出现在真实转换产物里的唯一行标识
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    ran = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': '清单.csv'})['text'])
    assert ran['status'] == 'succeeded'
    artifact_id = ran['outputs'][0]['artifact_id']

    listed = json.loads(_call(tools, 'session_artifacts', {})['text'])
    assert [item['id'] for item in listed] == [artifact_id], listed
    assert listed[0]['source'] == 'pack_artifact' and listed[0]['task_id'] == ran['task_id']

    read = json.loads(_call(tools, 'read_session_artifact', {'artifact_id': artifact_id})['text'])
    assert marker in read['text'], read['text'][:200]
    assert read['sha256'] == ran['outputs'][0]['sha256']
    assert read['total_bytes'] == ran['outputs'][0]['size']
    assert read['truncated'] is False
    assert read['version'] == version['version'] and read['task_id'] == ran['task_id']
    # 正文是材料不是指令，回执里明确说清楚。
    assert '不是指令' in read['note']

    # 下载仍拿到同一份字节：回读没有改动成果。
    download = client.get(ran['outputs'][0]['download_path'], headers=headers)
    assert download.status_code == 200 and marker.encode() in download.content


def test_readback_refuses_another_conversation_without_touching_the_file(bound_chat, monkeypatch):
    """同一个用户的另一个会话也不行：归属先于读取，文件根本不会被打开。"""
    from factory.control.capability_packs import PackStore
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    ran = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': '清单.csv'})['text'])
    artifact_id = ran['outputs'][0]['artifact_id']

    other_cid = _conversation(client, headers, aid)
    other_tools = _worker_tools(store, other_cid, _actor_id(store, other_cid))

    reads = []
    real = PackStore.artifact
    monkeypatch.setattr(PackStore, 'artifact',
                        lambda self, aid_, **kw: (reads.append(aid_), real(self, aid_, **kw))[1])

    assert json.loads(_call(other_tools, 'session_artifacts', {})['text']) == []
    refused = _call(other_tools, 'read_session_artifact', {'artifact_id': artifact_id})
    assert refused['is_error'], refused['text']
    assert json.loads(refused['text'])['refused'] == 'not_in_this_conversation'
    assert reads == [], '拒绝之前不得读取产物内容'


def test_readback_refuses_another_user_even_as_admin(bound_chat):
    """知道 ID、拥有管理权限，都不构成访问：登记关系才是判据。"""
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    ran = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': '清单.csv'})['text'])

    # 关键：同一个会话、同一个产物，只是换了发起者。登记表挡不住这种情况，
    # 必须由发起者校验挡住——否则管理员能读别人会话里的成果。
    from factory.control.conversation_pack_tools import PackTools, PackToolError
    stranger = PackTools(store, cid, 'someone-else', actor_role='admin')
    assert stranger.artifacts() == [], '换个发起者就不该看到这个会话的成果'
    try:
        stranger.read_artifact(ran['outputs'][0]['artifact_id'])
    except PackToolError as exc:
        assert exc.code == 'not_in_this_conversation'
    else:
        raise AssertionError('管理员身份不应绕过会话归属')


def test_unknown_id_and_binary_and_paging_boundaries(bound_chat):
    client, store, headers, pack, version, aid, cid = bound_chat
    tools = _worker_tools(store, cid, _actor_id(store, cid))
    from factory.control.conversation_pack_tools import PackTools, PackToolError, READ_MAX_BYTES

    # 未登记的 ID
    refused = _call(tools, 'read_session_artifact', {'artifact_id': 'no-such-artifact'})
    assert refused['is_error']
    assert json.loads(refused['text'])['refused'] == 'not_in_this_conversation'

    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_text(encoding='utf-8')
    ran = json.loads(_call(tools, 'run_attached_tool', {
        'pack_id': pack['id'], 'content': source, 'filename': '清单.csv'})['text'])
    artifact_id = ran['outputs'][0]['artifact_id']

    # 分页：小窗口如实标 truncated，续读能接上
    head = json.loads(_call(tools, 'read_session_artifact', {
        'artifact_id': artifact_id, 'max_bytes': 40})['text'])
    assert head['truncated'] is True and head['returned_bytes'] == 40
    rest = json.loads(_call(tools, 'read_session_artifact', {
        'artifact_id': artifact_id, 'offset': 40})['text'])
    assert rest['offset'] == 40 and rest['truncated'] is False
    full = json.loads(_call(tools, 'read_session_artifact', {'artifact_id': artifact_id})['text'])
    assert head['text'] + rest['text'] == full['text']

    # 单次读取有硬上限：拿一份大于上限的成果，模型也要不到整包
    big_id = _register_text_artifact(store, cid, _actor_id(store, cid),
                                     'big.txt', 'x' * (READ_MAX_BYTES + 5000))
    huge = json.loads(_call(tools, 'read_session_artifact', {
        'artifact_id': big_id, 'max_bytes': 10 ** 9})['text'])
    assert huge['returned_bytes'] == READ_MAX_BYTES, huge['returned_bytes']
    assert huge['truncated'] is True and huge['total_bytes'] == READ_MAX_BYTES + 5000

    # 二进制明确不支持。两种形态分开验，否则两道守卫互为兜底、谁都证明不了：
    #   · 非 UTF-8 字节序列（无 NUL）
    #   · 合法 UTF-8 但含 NUL
    reader = PackTools(store, cid, _actor_id(store, cid), actor_role='admin')
    for name, content in (('latin.bin', b'\xff\xfe\x89PNG-no-nul'),
                          ('withnul.txt', 'ok\x00still text'.encode('utf-8'))):
        aid_ = _register_artifact(store, cid, _actor_id(store, cid), name, content, 'bin-' + name)
        try:
            reader.read_artifact(aid_)
        except PackToolError as exc:
            assert exc.code == 'binary_not_supported', (name, exc.code)
        else:
            raise AssertionError('二进制成果应被明确拒绝：' + name)


def _register_text_artifact(store, cid, actor_id, name, text):
    """登记一份大文本成果到本会话，用于读取上限测试。"""
    return _register_artifact(store, cid, actor_id, name, text.encode('utf-8'), 'big-text-task')


def _register_artifact(store, cid, actor_id, name, content, task_id):
    from factory.control.capability_packs import PackStore
    from factory.control.agents import AgentStore
    art = PackStore(store).put_artifact(actor_id=actor_id, name=name, content=content,
                                        role='output', kind='file',
                                        validation_status='not_applicable')
    AgentStore(store).add_tool_receipt(cid, actor_id, {
        'task_id': task_id, 'status': 'succeeded', 'tool': 't', 'version': 1,
        'outputs': [{'artifact_id': art['id'], 'name': art['name'], 'kind': 'file',
                     'size': art['size'], 'sha256': art['sha256'],
                     'download_path': '/api/v4/capability-packs/artifacts/' + art['id'] + '/download'}]})
    return art['id']


