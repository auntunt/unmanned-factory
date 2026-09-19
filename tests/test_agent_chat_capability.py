"""Gap #1: the no-project daily-chat path freezes the role/instructions/skill
version to the conversation and mounts only that role's granted materials
(bounded, read-only), with isolation, and without creating a project or run."""
import io
import time
import zipfile

from factory.control.providers import ProviderResult
from factory.control.agents import AgentStore
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401


def _skill(body='报价规则：单价乘以数量，再按等级打折。', ref='参考：VIP 客户 9 折。'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('SKILL.md', body)
        z.writestr('refs/rule.md', ref)
    return buf.getvalue()


def _agent(client, headers, service, name, skill=None):
    a = client.post('/api/v4/agents', json={'name': name, 'purpose': name}, headers=headers).json()
    if skill is not None:
        up = client.post(f"/api/v4/agents/{a['id']}/skills", files={'file': ('s.zip', skill, 'application/zip')}, headers=headers)
        assert up.status_code == 201, up.text
        assets = client.get(f"/api/v4/agents/{a['id']}/skills", headers=headers).json()['skills']
        draft = service.agents.save_draft(a['id'], {'skill_ids': [assets[0]['id']]}, 0)
        service.agents.apply(a['id'], draft['revision'], 'tester')
    return a


def _capture(monkeypatch, service):
    reqs = []
    def fake_run(req, emit, cancel):
        reqs.append(req)
        return ProviderResult(text='已根据已挂载资料作答')
    monkeypatch.setattr(service.runner, 'run', fake_run)
    return reqs


def _send(client, headers, cid, content):
    return client.post(f'/api/v4/conversations/{cid}/messages', json={'content': content}, headers=headers)


def _wait_job(service, store, job_id, deadline=5):
    end = time.time() + deadline
    while time.time() < end and service.maintenance_status(job_id)['status'] in ('pending', 'running', 'cancel_requested'):
        time.sleep(.02)
    return service.maintenance_status(job_id)['status']


def _do_convo(client, headers, aid):
    r = client.post(f'/api/v4/agents/{aid}/conversations', json={'mode': 'do'}, headers=headers)
    assert r.status_code == 201
    return r.json()['id']


def test_chat_freezes_version_and_mounts_only_this_role_materials(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    quote = _agent(client, headers, service, '报价助手', skill=_skill())
    other = _agent(client, headers, service, '会议总结助手', skill=_skill(body='会议纪要方法', ref='无关资料'))
    reqs = _capture(monkeypatch, service)

    cid = _do_convo(client, headers, quote['id'])
    first = _send(client, headers, cid, '这单报价多少？')
    assert first.status_code == 201 and first.json()['run'] is None  # daily chat: no run
    _wait_job(service, store, first.json()['job_id'])

    # A run was never created for a no-project chat.
    assert store.runs() == [] or all(r.get('source', {}).get('conversation_id') != cid for r in store.all_runs())
    # The mount carried THIS role's skill material, not the other role's.
    req = reqs[-1]
    assert req.reference_mount is not None
    texts = ' '.join(d.get('text', '') for d in req.reference_mount['documents'])
    assert '报价规则' in texts and '会议纪要方法' not in texts
    # The prompt used the frozen compiled instructions, not a live re-read.
    assert AgentStore(store).conversation_snapshot(cid)['version'] == 2

    # A different role's conversation only sees its own materials (isolation).
    cid2 = _do_convo(client, headers, other['id'])
    _send(client, headers, cid2, '帮我总结'); _wait_job(service, store, AgentStore(store).conversation(cid2)['messages'][-1]['job_id'])
    other_texts = ' '.join(d.get('text', '') for d in (reqs[-1].reference_mount or {'documents': []})['documents'])
    assert '会议纪要方法' in other_texts and '报价规则' not in other_texts


def test_role_update_does_not_change_an_existing_conversation(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    agents = AgentStore(store)
    reqs = _capture(monkeypatch, service)

    cid = _do_convo(client, headers, agent['id'])
    _wait_job(service, store, _send(client, headers, cid, '第一问').json()['job_id'])
    assert agents.conversation_snapshot(cid)['version'] == 1

    # Publish a new agent version (v2) after the conversation started.
    draft = agents.save_draft(agent['id'], {'instructions': '新版说明'}, 0)
    agents.apply(agent['id'], draft['revision'], 'tester')
    assert agents.version(agent['id'])['version'] == 2

    # The existing conversation keeps v1; a new conversation freezes v2.
    _wait_job(service, store, _send(client, headers, cid, '第二问').json()['job_id'])
    assert agents.conversation_snapshot(cid)['version'] == 1
    cid_new = _do_convo(client, headers, agent['id'])
    _wait_job(service, store, _send(client, headers, cid_new, '新会话').json()['job_id'])
    assert agents.conversation_snapshot(cid_new)['version'] == 2


def test_new_message_while_answering_is_rejected_and_draft_kept(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    import threading
    release = threading.Event()
    def slow(req, emit, cancel):
        release.wait(2); return ProviderResult(text='ok')
    monkeypatch.setattr(service.runner, 'run', slow)
    cid = _do_convo(client, headers, agent['id'])
    a = _send(client, headers, cid, '问一次')
    assert a.status_code == 201 and a.json()['status'] == 'pending'
    before = len(AgentStore(store).conversation(cid)['messages'])
    b = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '这是一条新问题'}, headers=headers)
    # A different message during answering is rejected (busy), NOT persisted and NOT
    # answered by the old job.
    assert b.status_code == 201 and b.json().get('busy') is True
    msgs = AgentStore(store).conversation(cid)['messages']
    assert len(msgs) == before and all(m.get('content') != '这是一条新问题' for m in msgs)
    release.set(); _wait_job(service, store, a.json()['job_id'])


def test_same_idempotency_key_never_double_saves_or_double_answers(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    calls = []
    def once(req, emit, cancel):
        calls.append(1); return ProviderResult(text='唯一回答')
    monkeypatch.setattr(service.runner, 'run', once)
    cid = _do_convo(client, headers, agent['id'])
    key = 'clientkey-123456'
    first = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '报价问题', 'idempotency_key': key}, headers=headers)
    assert first.status_code == 201 and first.json()['status'] == 'pending'
    _wait_job(service, store, first.json()['job_id'])
    # Retry the SAME request after completion: idempotent replay, no re-save, no re-answer.
    replay = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '报价问题', 'idempotency_key': key}, headers=headers)
    assert replay.status_code == 201 and replay.json().get('idempotent_replay') is True
    user_msgs = [m for m in AgentStore(store).conversation(cid)['messages'] if m['role'] == 'user']
    assert sum(m.get('content') == '报价问题' for m in user_msgs) == 1
    assert len(calls) == 1


def test_cross_user_and_cross_conversation_access_denied(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    _capture(monkeypatch, service)
    cid = _do_convo(client, headers, agent['id'])
    # A second, non-admin member must not read or post to another user's conversation.
    client.app.state.auth.create_user('mallory', 'another-long-password', role='member')
    other = client.post('/api/auth/login', json={'username': 'mallory', 'password': 'another-long-password'}, headers={'Origin': 'http://testserver'})
    mh = {'Origin': 'http://testserver', 'X-CSRF-Token': other.json()['csrf_token']}
    assert client.get(f'/api/v4/conversations/{cid}', headers=mh).status_code == 403
    assert client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '偷看'}, headers=mh).status_code == 403


def test_mount_failure_does_not_call_the_model(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手', skill=_skill())
    called = []
    monkeypatch.setattr(service.runner, 'run', lambda req, emit, cancel: called.append(1) or ProviderResult(text='x'))
    import factory.control.mounts as mounts
    monkeypatch.setattr(mounts, 'compile_mounts', lambda *a, **k: (_ for _ in ()).throw(ValueError('资料损坏')))
    cid = _do_convo(client, headers, agent['id'])
    r = _send(client, headers, cid, '报价问题')
    # Fail-closed: a failed assistant message, and the model was never invoked.
    assert r.status_code == 201
    msgs = AgentStore(store).conversation(cid)['messages']
    assert any(m['role'] == 'assistant' and m.get('status') == 'failed' for m in msgs)
    assert called == []


def test_conversation_snapshot_hidden_in_list_detail_and_message(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手', skill=_skill())
    _capture(monkeypatch, service)
    cid = _do_convo(client, headers, agent['id'])
    msg = _send(client, headers, cid, '报价问题'); _wait_job(service, store, msg.json()['job_id'])
    # The internal frozen snapshot leaks in none of the three conversation responses.
    lst = client.get(f"/api/v4/agents/{agent['id']}/conversations", headers=headers).json()['conversations']
    assert lst and all('agent_snapshot' not in c for c in lst)
    detail = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    assert 'agent_snapshot' not in detail
    assert 'agent_snapshot' not in msg.json()['conversation']


def test_chat_answer_can_be_cancelled_then_retried(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    import threading
    gate = threading.Event()
    from factory.control.providers import ProviderCancelled
    def slow(req, emit, cancel):
        gate.wait(2)
        if cancel.is_set(): raise ProviderCancelled('调用已取消')
        return ProviderResult(text='ok')
    monkeypatch.setattr(service.runner, 'run', slow)
    cid = _do_convo(client, headers, agent['id'])
    job = _send(client, headers, cid, '问一次').json()['job_id']
    cancelled = client.post(f'/api/v4/maintenance-jobs/{job}/cancel', headers=headers)
    assert cancelled.status_code == 200
    gate.set()
    assert _wait_job(service, store, job) == 'cancelled'
    # Retry by asking again in the same conversation; a fresh answer is produced.
    monkeypatch.setattr(service.runner, 'run', lambda req, emit, cancel: ProviderResult(text='重试后的回答'))
    again = _send(client, headers, cid, '再问一次')
    assert again.status_code == 201 and again.json()['status'] == 'pending'
    assert _wait_job(service, store, again.json()['job_id']) == 'completed'
    msgs = AgentStore(store).conversation(cid)['messages']
    assert any(m['role'] == 'assistant' and m.get('content') == '重试后的回答' for m in msgs)


def test_session_attachment_is_read_by_this_conversation_only(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    reqs = _capture(monkeypatch, service)
    cid = _do_convo(client, headers, agent['id'])
    up = client.post(f'/api/v4/conversations/{cid}/attachments',
                     files={'file': ('quote.csv', '客户,单价,数量\n张三,100,3'.encode(), 'text/csv')}, headers=headers)
    assert up.status_code == 201
    # attachment metadata comes back, but the body is not exposed to the client
    assert up.json()['attachment']['name'] == 'quote.csv' and 'text' not in up.json()['attachment']
    assert all('text' not in a for a in up.json()['conversation'].get('attachments', []))

    _wait_job(service, store, _send(client, headers, cid, '按附件算总价').json()['job_id'])
    texts = ' '.join(d.get('text', '') for d in (reqs[-1].reference_mount or {'documents': []})['documents'])
    assert '张三,100,3' in texts
    assert 'quote.csv' in reqs[-1].prompt
    assert 'attachment/' + up.json()['attachment']['id'] in reqs[-1].prompt
    assert '张三,100,3' not in reqs[-1].prompt  # inventory only; body stays behind read tools

    # A different conversation of the same role does not see this attachment.
    cid2 = _do_convo(client, headers, agent['id'])
    _wait_job(service, store, _send(client, headers, cid2, '你好').json()['job_id'])
    other = reqs[-1].reference_mount
    assert 'quote.csv' not in reqs[-1].prompt
    assert other is None or all('张三,100,3' not in d.get('text', '') for d in other['documents'])


def test_attachment_upload_rejects_non_owner_and_non_utf8(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])
    # Non-UTF8 upload rejected (owner session, before any other login swaps the cookie).
    assert client.post(f'/api/v4/conversations/{cid}/attachments', files={'file': ('x.bin', b'\xff\xfe\x00', 'application/octet-stream')}, headers=headers).status_code == 422
    # A different member cannot attach to someone else's conversation.
    client.app.state.auth.create_user('mallory2', 'another-long-password', role='member')
    other = client.post('/api/auth/login', json={'username': 'mallory2', 'password': 'another-long-password'}, headers={'Origin': 'http://testserver'})
    mh = {'Origin': 'http://testserver', 'X-CSRF-Token': other.json()['csrf_token']}
    assert client.post(f'/api/v4/conversations/{cid}/attachments', files={'file': ('x.txt', b'hi', 'text/plain')}, headers=mh).status_code == 403


def test_conversation_calc_is_deterministic_and_verifiable(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])
    r = client.post(f'/api/v4/conversations/{cid}/calc',
                    json={'items': [{'name': '演示', 'unit_price': '100', 'quantity': '3'}], 'discount_rate': '0.9'}, headers=headers)
    assert r.status_code == 200 and r.json()['total'] == '270.00' and r.json()['subtotal'] == '300.00'
    # bad input is a clear 422, not a fabricated number
    assert client.post(f'/api/v4/conversations/{cid}/calc', json={'items': []}, headers=headers).status_code == 422


def test_document_export_is_a_controlled_scoped_write_no_run(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '会议总结助手')
    cid = _do_convo(client, headers, agent['id'])
    exp = client.post(f'/api/v4/conversations/{cid}/export',
                      json={'title': '会议纪要', 'format': 'md', 'content': '# 纪要\n- 决定：上线延后'}, headers=headers)
    assert exp.status_code == 201
    eid = exp.json()['export']['id']
    assert 'content' not in exp.json()['export']  # body not echoed
    dl = client.get(f'/api/v4/conversations/{cid}/exports/{eid}/download', headers=headers)
    assert dl.status_code == 200 and '决定：上线延后' in dl.text and 'attachment' in dl.headers['content-disposition']
    # No project or coding run was created by exporting.
    assert store.runs() == []


def test_quote_and_summary_agents_multi_turn_with_isolation(app_env, monkeypatch):
    """Local acceptance for the two named roles: multi-turn chat over their own
    materials, deterministic quote, controlled export, and material isolation.
    Real-model answer correctness is verified by Codex with a live model."""
    client, store, service, repo = app_env
    headers = login(client)
    quote_agent = _agent(client, headers, service, '报价助手', skill=_skill(body='报价规则：单价×数量，VIP 打 9 折。'))
    summary_agent = _agent(client, headers, service, '会议总结助手', skill=_skill(body='纪要方法：只写记录中的事实，标注推断与未提供。'))
    reqs = _capture(monkeypatch, service)

    # 报价助手：多轮 + 附件 + 确定性计算 + 导出
    q = _do_convo(client, headers, quote_agent['id'])
    client.post(f'/api/v4/conversations/{q}/attachments', files={'file': ('prices.csv', '项目,单价,数量\n演示,100,3'.encode(), 'text/csv')}, headers=headers)
    _wait_job(service, store, _send(client, headers, q, '这单报价多少？').json()['job_id'])
    _wait_job(service, store, _send(client, headers, q, '客户是 VIP，再算一次').json()['job_id'])
    qtexts = ' '.join(d.get('text', '') for d in (reqs[-1].reference_mount or {'documents': []})['documents'])
    assert '报价规则' in qtexts and '演示,100,3' in qtexts and '纪要方法' not in qtexts  # only its own materials
    calc = client.post(f'/api/v4/conversations/{q}/calc', json={'items': [{'name': '演示', 'unit_price': '100', 'quantity': '3'}], 'discount_rate': '0.9'}, headers=headers)
    assert calc.json()['total'] == '270.00'  # verifiable, from the given rule
    exp = client.post(f'/api/v4/conversations/{q}/export', json={'title': '报价单', 'format': 'md', 'content': '# 报价单\n总价：270.00（VIP 9 折）'}, headers=headers)
    assert exp.status_code == 201

    # 会议总结助手：多轮，独立资料
    s = _do_convo(client, headers, summary_agent['id'])
    client.post(f'/api/v4/conversations/{s}/attachments', files={'file': ('minutes.txt', '张三：预算是十万。李四：下周上线。'.encode(), 'text/plain')}, headers=headers)
    _wait_job(service, store, _send(client, headers, s, '帮我总结要点').json()['job_id'])
    _wait_job(service, store, _send(client, headers, s, '有没有提到验收标准？').json()['job_id'])
    stexts = ' '.join(d.get('text', '') for d in (reqs[-1].reference_mount or {'documents': []})['documents'])
    assert '纪要方法' in stexts and '预算是十万' in stexts and '报价规则' not in stexts  # isolation

    # 两个角色的会话各自独立，互不串资料；日常聊天全程未创建开发运行。
    assert store.runs() == []


def test_chat_loop_model_invokes_bound_calc_and_export_tools(app_env, monkeypatch):
    """From a user message, the (fake) model invokes the session-bound tools: calc
    returns a verifiable total, export writes a downloadable doc to THIS conversation.
    Covers tool registration on the request, invocation, result and the download loop."""
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])

    captured = {}
    def model(req, emit, cancel):
        # The request carries only a serializable binding; the trusted side rebuilds
        # the tools from it, bound to this conversation + user.
        from factory.control.conversation_tools import ConversationTools
        assert req.conversation_binding is not None
        tools = ConversationTools.from_binding(req.conversation_binding)
        assert tools.cid == cid
        result = tools.calc([{'name': '演示', 'unit_price': '100', 'quantity': '3'}], '0.9')
        captured['total'] = result['total']
        item = tools.export('报价单', 'md', f"# 报价单\n总价：{result['total']}")
        captured['export_id'] = item['id']
        from factory.control.providers import ProviderResult
        return ProviderResult(text=f"总价 {result['total']}，已生成报价单。")
    monkeypatch.setattr(service.runner, 'run', model)

    _wait_job(service, store, _send(client, headers, cid, '按 3 台演示、VIP 9 折报价').json()['job_id'])
    assert captured['total'] == '270.00'  # deterministic tool result
    # The exported file is downloadable from this conversation.
    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    assert any(e['id'] == captured['export_id'] for e in conv.get('exports', []))
    dl = client.get(f"/api/v4/conversations/{cid}/exports/{captured['export_id']}/download", headers=headers)
    assert dl.status_code == 200 and '270.00' in dl.text
    assert store.runs() == []  # daily chat, no dev run


def test_bound_tool_validates_input_and_cannot_target_another_user(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])
    seen = {}
    def model(req, emit, cancel):
        from factory.control.conversation_tools import ConversationTools
        tools = ConversationTools.from_binding(req.conversation_binding)
        import pytest as _p
        with _p.raises(ValueError):
            tools.calc([], '1')  # invalid params rejected by the tool
        # The binding is fixed: the tool writes only to its own conversation/actor.
        seen['bound'] = (tools.cid, tools.actor_id)
        from factory.control.providers import ProviderResult
        return ProviderResult(text='ok')
    monkeypatch.setattr(service.runner, 'run', model)
    _wait_job(service, store, _send(client, headers, cid, '算一下').json()['job_id'])
    assert seen['bound'][0] == cid


def test_idempotent_replay_reports_real_state_not_fake_completed(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    monkeypatch.setattr(service.runner, 'run', lambda req, emit, cancel: (_ for _ in ()).throw(RuntimeError('模型出错')))
    cid = _do_convo(client, headers, agent['id'])
    key = 'stablekey-abcdef'
    r = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '会失败的问题', 'idempotency_key': key}, headers=headers)
    assert _wait_job(service, store, r.json()['job_id']) == 'failed'
    # Replaying the same key reports the REAL failed state, not a fabricated 'completed'.
    replay = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '会失败的问题', 'idempotency_key': key}, headers=headers)
    assert replay.json()['idempotent_replay'] is True and replay.json()['status'] == 'failed'
    # Same key, different content is rejected explicitly.
    diff = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '换了内容', 'idempotency_key': key}, headers=headers)
    assert diff.status_code == 409


class _ResultMessage:
    is_error = False
    result = 'done'
    session_id = 'exec-test'
    total_cost_usd = 0


def _capture_req(client, headers, service, monkeypatch, cid):
    """Drive one real chat turn and capture the ProviderRequest the route builds,
    so executor tests run against a genuinely bound ConversationTools."""
    captured = {}
    def grab(req, emit, cancel):
        captured['req'] = req
        return ProviderResult(text='ok')
    monkeypatch.setattr(service.runner, 'run', grab)
    _wait_job(service, None, _send(client, headers, cid, '算一下').json()['job_id'])
    return captured['req']


def test_executor_registers_and_gates_bound_session_tools(app_env, monkeypatch):
    """Executor layer: the session MCP server is registered, and the PreToolUse hook
    plus can_use_tool allow calc/export ONLY because this request is bound; unknown
    MCP tools are denied. This is the gate that rejected calc before the fix."""
    import pytest
    sdk = pytest.importorskip('claude_agent_sdk')
    from factory.control.providers import _run_claude
    from factory.control.conversation_tools import TOOL_NAMES
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])
    req = _capture_req(client, headers, service, monkeypatch, cid)
    assert req.conversation_binding is not None
    import json as _json
    from dataclasses import asdict as _asdict
    _json.dumps(_asdict(req))  # the request must cross the JSONL process boundary

    seen = {}
    async def query(*, prompt, options):
        assert 'session' in options.mcp_servers                       # registration
        assert set(TOOL_NAMES) <= set(options.allowed_tools)
        hook = options.hooks['PreToolUse'][0].hooks[0]
        allow_calc = await hook({'tool_name': 'mcp__session__calc', 'tool_input': {}}, None, {})
        allow_export = await hook({'tool_name': 'mcp__session__export', 'tool_input': {}}, None, {})
        deny_unknown = await hook({'tool_name': 'mcp__evil__do', 'tool_input': {}}, None, {})
        assert allow_calc['hookSpecificOutput']['permissionDecision'] == 'allow'
        assert allow_export['hookSpecificOutput']['permissionDecision'] == 'allow'
        assert deny_unknown['hookSpecificOutput']['permissionDecision'] == 'deny'
        can = await options.can_use_tool('mcp__session__calc', {}, {})           # can_use_tool
        assert type(can).__name__ == 'PermissionResultAllow'
        cannot = await options.can_use_tool('mcp__evil__do', {}, {})
        assert type(cannot).__name__ == 'PermissionResultDeny'
        seen['ok'] = True
        yield _ResultMessage()
    monkeypatch.setattr(sdk, 'query', query)
    result = _run_claude(req, lambda *e: None)
    assert result.text == 'done' and seen.get('ok')


def test_executor_denies_session_tools_when_request_is_unbound(app_env, monkeypatch):
    """Same executor, no binding: the session server is not registered and the gate
    denies calc/export. The whitelist never globally allows MCP tools."""
    import dataclasses
    import pytest
    sdk = pytest.importorskip('claude_agent_sdk')
    from factory.control.providers import _run_claude
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])
    bound = _capture_req(client, headers, service, monkeypatch, cid)
    unbound = dataclasses.replace(bound, conversation_binding=None)

    seen = {}
    async def query(*, prompt, options):
        assert 'session' not in options.mcp_servers
        hook = options.hooks['PreToolUse'][0].hooks[0]
        denied = await hook({'tool_name': 'mcp__session__calc', 'tool_input': {}}, None, {})
        assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'
        seen['ok'] = True
        yield _ResultMessage()
    monkeypatch.setattr(sdk, 'query', query)
    _run_claude(unbound, lambda *e: None)
    assert seen.get('ok')


def test_real_session_mcp_server_calc_and_export_handlers(app_env, monkeypatch):
    """The actual @tool handlers over the real SDK MCP protocol: calc returns a
    verifiable total, invalid params return is_error, and export writes a
    downloadable document to THIS conversation (permission enforced by binding)."""
    import asyncio
    import json
    import pytest
    pytest.importorskip('claude_agent_sdk')
    from factory.control import conversation_tools as ct
    client, store, service, repo = app_env
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])
    req = _capture_req(client, headers, service, monkeypatch, cid)
    tools = ct.ConversationTools.from_binding(req.conversation_binding)
    events = []
    server = ct.create_server(tools, lambda *e: events.append(e))
    assert server['name'] == 'session'

    export_id = {}
    async def roundtrip():
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
                # 普通会话的真实清单：算账、导出，加上「本角色已挂靠能力包」的
                # 列举与执行。后两件由挂靠关系限定范围，未挂靠即为空清单。
                assert {t.name for t in inventory.tools} == {
                    'calc', 'export', 'attached_tools', 'run_attached_tool'}
                ok = await session.call_tool('calc', {'items': [{'name': '演示', 'unit_price': '100', 'quantity': '3'}], 'discount_rate': '0.9'})
                assert not ok.is_error
                assert json.loads(ok.content[0].text)['total'] == '270.00'          # verifiable result
                bad = await session.call_tool('calc', {'items': []})
                assert bad.is_error                                                   # param validation
                wrote = await session.call_tool('export', {'title': '报价单', 'format': 'md', 'content': '# 报价单\n总价：270.00'})
                assert not wrote.is_error
                export_id['id'] = json.loads(wrote.content[0].text.split('：', 1)[1])['id']
            tasks.cancel_scope.cancel()
    asyncio.run(roundtrip())
    # The document the handler wrote is downloadable from this conversation.
    dl = client.get(f"/api/v4/conversations/{cid}/exports/{export_id['id']}/download", headers=headers)
    assert dl.status_code == 200 and '270.00' in dl.text
    assert any(e[0] == 'session.calc' for e in events)
    assert any(e[0] == 'session.export' for e in events)


def test_tools_disabled_with_conversation_tools_is_rejected(app_env):
    """A no-tools request must not silently keep the session tools; the executor
    rejects the combination instead of only guarding the system_prompt type."""
    import pytest
    from factory.control.providers import _run_claude, ProviderRequest, ProviderError
    with pytest.raises(ProviderError):
        _run_claude(ProviderRequest('claude', 'test', 'goal', '/tmp', read_only=True,
                                     tools_disabled=True,
                                     conversation_binding={'db_path': '/tmp/x.db', 'conversation_id': 'c', 'actor_id': 'u'}), lambda *e: None)


def test_codex_executor_rejects_unconsumed_session_tools(app_env):
    """The Codex executor does not consume conversation_tools; it must fail loudly
    rather than silently drop the calc/export capability."""
    import pytest
    from factory.control.providers import _run_codex, ProviderRequest, ProviderError
    with pytest.raises(ProviderError):
        _run_codex(ProviderRequest('codex', 'test', 'goal', '/tmp', read_only=True,
                                   conversation_binding={'db_path': '/tmp/x.db', 'conversation_id': 'c', 'actor_id': 'u'}), lambda *e: None)


def test_process_level_chat_tool_chain_exports_through_real_worker(app_env, monkeypatch):
    """End-to-end across the process boundary: chat endpoint -> real SDKRunner ->
    JSONL -> sdk_worker subprocess -> ConversationTools rebuilt from the serializable
    binding -> session calc/export tool calls -> export written to the SQLite file ->
    downloadable from this conversation. Only the external model response is replaced
    (in tests/proc_chat_worker.py); the runner and process comms are the real ones.
    Guards the reported 'ConversationTools is not JSON serializable' failure."""
    import pytest
    pytest.importorskip('claude_agent_sdk')
    from factory.control.providers import SDKRunner
    client, store, service, repo = app_env
    # A real SDKRunner, only pointed at a worker module that stubs the model.
    service.runner = SDKRunner(worker_module='tests.proc_chat_worker')
    service.governance = None  # exercise the runner directly, no budget broker in this test
    # Chat uses the planner profile; the session tool chain runs on the claude executor.
    cfg = service.runtime_settings.get()
    profs = dict(cfg['profiles'])
    profs['planner'] = {'provider': 'claude', 'model': 'test'}
    service.runtime_settings.update({'profiles': profs, 'limits': cfg['limits']}, cfg['revision'], 'tester')
    headers = login(client)
    agent = _agent(client, headers, service, '报价助手')
    cid = _do_convo(client, headers, agent['id'])

    r = _send(client, headers, cid, '按 3 台演示、VIP 9 折报价，并生成报价单文件')
    assert r.status_code in (200, 201), r.text
    status = _wait_job(service, store, r.json()['job_id'], deadline=30)
    assert status == 'completed', f'job ended {status}'

    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    exports = conv.get('exports', [])
    assert exports, 'the worker export did not land in the conversation'
    eid = exports[0]['id']
    dl = client.get(f'/api/v4/conversations/{cid}/exports/{eid}/download', headers=headers)
    assert dl.status_code == 200 and '270.00' in dl.text  # verifiable total, written cross-process
    # The assistant reply (produced in the worker) reflects the tool result.
    msgs = conv['messages']
    assert any(m['role'] == 'assistant' and '270.00' in (m.get('content') or '') for m in msgs)
    assert store.runs() == []  # daily chat: no dev run created
