import json
import threading
from concurrent.futures import ThreadPoolExecutor
from tests.test_chat_attached_tool import bound_chat, app_env, _actor_id
from factory.control.conversation_pack_tools import PackTools
from factory.control.capability_packs import PackStore

def test_cancel_endpoint_reaches_chat_tool_executor(bound_chat, monkeypatch):
    client, store, headers, pack, version, aid, cid = bound_chat
    entered = threading.Event()
    release = threading.Event()
    def blocked(version, files, inputs, *, cancel=None, **kwargs):
        entered.set()
        assert release.wait(5)
        cancelled = cancel is not None and cancel.is_set()
        return {'status': 'cancelled' if cancelled else 'succeeded', 'outputs': [],
                'validation_status': 'unverified' if cancelled else 'passed',
                'evidence': {}, 'error_code': 'cancelled' if cancelled else None}
    monkeypatch.setattr('factory.control.conversation_pack_tools.run_tool', blocked)
    tool = PackTools(store, cid, _actor_id(store, cid), actor_role='admin')
    with ThreadPoolExecutor() as pool:
        future = pool.submit(tool.run, pack['id'], 'test input', 'sample.txt')
        assert entered.wait(5)
        with store.connect() as db:
            rows = db.execute('SELECT data FROM pack_tasks').fetchall()
        task = next(json.loads(r['data']) for r in rows if json.loads(r['data'])['agent_id'] == aid)
        try:
            response = client.post(f"/api/v4/capability-packs/invocations/{task['id']}/cancel", headers=headers)
            assert response.status_code == 200, response.text
            assert PackStore(store).task(task['id'])['status'] == 'cancel_requested'
        finally:
            release.set()
        result = future.result(5)
    assert result['status'] == 'cancelled', result
