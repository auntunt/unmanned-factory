import json
import threading
import time

import pytest

from factory.control.agents import AgentStore
from factory.control.knowledge import KnowledgeStore
from factory.control.maintenance import run_maintenance
from factory.control.project_assistants import ProjectAssistants
from factory.control.providers import ProviderError, ProviderRequest, ProviderResult, ProviderCancelled
from factory.control.store import Conflict
from tests.test_control_app import login
from tests.test_workbench_app import app_env, project


def setup_project(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    helpers = ProjectAssistants(store)
    a = helpers.agents.create({'name': '老项目翻新', 'instructions': '保留既有兼容性', 'acceptance': ['通过原有检查']}, 'owner')
    return client, store, service, headers, p, helpers, a


def test_project_binding_freezes_agent_and_models_for_normal_run(app_env, monkeypatch):
    client, store, service, headers, p, helpers, a = setup_project(app_env)
    response = client.put(f"/api/v4/projects/{p['id']}/assistant", headers=headers, json={'agent_id': a['id'], 'expected_revision': 0})
    assert response.status_code == 200
    monkeypatch.setattr(service, '_submit', lambda *args: None)
    response = client.post('/api/v2/runs', headers=headers, json={'project_id': p['id'], 'request': '修复兼容性'})
    run = store.get(response.json()['id'])
    assert run['agent_id'] == a['id']
    assert run['agent_snapshot']['instructions'] == '保留既有兼容性'
    draft = helpers.agents.save_draft(a['id'], {'instructions': '新的做法'}, 0)
    helpers.agents.apply(a['id'], draft['revision'])
    assert store.get(run['id'])['agent_snapshot']['version'] == 1
    assert helpers.binding(p['id'])['agent']['version']['version'] == 2
    assert client.put(f"/api/v4/projects/{p['id']}/assistant", headers=headers, json={'agent_id': None, 'expected_revision': 0}).status_code == 409


def test_learning_merge_is_atomic_idempotent_and_preserves_conflicts(app_env):
    client, store, service, headers, p, helpers, a = setup_project(app_env)
    entry = helpers.memory.put_entry(p['id'], {'title': '先确认协议版本', 'content': '旧格式应先读取版本再选择转换器。', 'kind': 'decision', 'status': 'candidate'}, 'owner')
    draft = helpers.agents.save_draft(a['id'], {}, 0, conflicts=['需要决定适用版本'])
    body = {'source_id': 'knowledge:' + entry['key'], 'source_revision': 1, 'destination': 'agent', 'agent_id': a['id'], 'draft_revision': 0}
    url = f"/api/v4/projects/{p['id']}/learnings/settle"
    assert client.post(url, headers=headers, json=body).status_code == 409
    assert helpers.learnings(p['id'])[0].get('disposition') is None
    body['draft_revision'] = draft['revision']
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert client.post(url, headers=headers, json=body).json() == response.json()
    draft = helpers.agents.draft(a['id'])
    assert draft['revision'] == 2
    assert draft['conflicts'] == ['需要决定适用版本']
    assert '先确认协议版本' in draft['patch']['instructions']
    assert helpers.agents.version(a['id'])['instructions'] == '保留既有兼容性'
    assert helpers.learnings(p['id'])[0]['disposition']['applied'] is False
    draft = helpers.agents.save_draft(a['id'], {}, 2, conflicts=[])
    helpers.agents.apply(a['id'], draft['revision'])
    assert helpers.learnings(p['id'])[0]['disposition']['applied'] is True


def test_standalone_keeps_source_and_does_not_modify_agent(app_env):
    _, _, _, _, p, helpers, a = setup_project(app_env)
    entry = helpers.memory.put_entry(p['id'], {'title': '项目约束', 'content': '仅适用于这个客户', 'kind': 'decision', 'status': 'candidate'}, 'owner')
    saved = helpers.settle(p['id'], 'knowledge:' + entry['key'], 1, 'standalone', None, 0, 'owner')
    assert saved['source']['content'] == '仅适用于这个客户'
    assert helpers.agents.draft(a['id'])['revision'] == 0
    with pytest.raises(KeyError):
        helpers.settle(p['id'], 'knowledge:missing', 1, 'standalone', None, 0, 'owner')


def test_overload_retries_and_cancellation_are_bounded():
    class Cancel:
        def is_set(self): return False
        def wait(self, delay): return False
    class Runner:
        calls = 0
        def run(self, request, emit, cancel):
            self.calls += 1
            if self.calls < 3: raise ProviderError('API Error: Repeated 529 Overloaded')
            return ProviderResult('ok')
    runner = Runner(); retries = []
    request = ProviderRequest('claude', 'test', 'material', '/tmp', timeout_s=30, read_only=True)
    assert run_maintenance(runner, request, lambda *_: None, Cancel(), retries.append).text == 'ok'
    assert runner.calls == 3 and retries == [1, 2]
    class Broken:
        def run(self, *args): raise ProviderError('401 unauthorized')
    with pytest.raises(ProviderError, match='401'):
        run_maintenance(Broken(), request, lambda *_: None, Cancel(), retries.append)
    assert retries == [1, 2]
    cancel = threading.Event(); cancel.set()
    with pytest.raises(ProviderCancelled):
        run_maintenance(runner, request, lambda *_: None, cancel, retries.append)


def test_failed_maintenance_retry_reuses_saved_material(app_env, monkeypatch):
    client, store, service, headers, p, helpers, a = setup_project(app_env)
    c = client.post(f"/api/v4/agents/{a['id']}/conversations", headers=headers, json={'mode': 'maintain'}).json()
    def fail(*args): raise ProviderError('529 Overloaded')
    monkeypatch.setattr('factory.control.maintenance.run_maintenance', fail)
    first = client.post(f"/api/v4/conversations/{c['id']}/messages", headers=headers, json={'content': '保留现有依赖版本'})
    job_id = first.json()['job_id']
    deadline = time.monotonic() + 3
    while service.maintenance_status(job_id)['status'] not in ('failed', 'completed') and time.monotonic() < deadline: time.sleep(.01)
    assert service.maintenance_status(job_id)['status'] == 'failed'
    seen = []
    def success(runner, request, *args):
        seen.append(request.prompt)
        return ProviderResult(json.dumps({'patch': {'instructions': '保留依赖版本'}, 'conflicts': []}))
    monkeypatch.setattr('factory.control.maintenance.run_maintenance', success)
    second = client.post(f"/api/v4/conversations/{c['id']}/retry", headers=headers)
    assert second.status_code == 201, second.text
    deadline = time.monotonic() + 3
    while service.maintenance_status(second.json()['job_id'])['status'] not in ('failed', 'completed') and time.monotonic() < deadline: time.sleep(.01)
    assert service.maintenance_status(second.json()['job_id'])['status'] == 'completed'
    assert '保留现有依赖版本' in seen[0]
    assert len([m for m in helpers.agents.conversation(c['id'])['messages'] if m['role'] == 'user']) == 1


def test_workspace_selection_and_standalone_skill_roundtrip(app_env):
    import io, zipfile
    client, store, service, headers, p, helpers, a = setup_project(app_env)
    body = {'name': '翻新工作区', 'budget_usd': 12, 'idempotency_key': 'helper-workspace-test', 'agent_id': a['id']}
    response = client.post('/api/v2/projects/create-workspace', headers=headers, json=body)
    assert response.status_code == 201, response.text
    pid = response.json()['id']
    assert helpers.binding(pid)['agent_id'] == a['id']
    assert client.post('/api/v2/projects/create-workspace', headers=headers, json=body).json()['id'] == pid
    assert helpers.binding(pid)['revision'] == 1
    count = len(store.projects())
    assert client.post('/api/v2/projects/create-workspace', headers=headers, json={**body, 'idempotency_key': 'bad-helper-workspace', 'agent_id': 'missing'}).status_code == 404
    assert len(store.projects()) == count
    entry = helpers.memory.put_entry(pid, {'title': '独立做法', 'content': '读取版本字段再处理', 'kind': 'decision', 'status': 'candidate'}, 'owner')
    source_id = 'knowledge:' + entry['key']
    helpers.settle(pid, source_id, 1, 'standalone', None, 0, 'owner')
    exported = client.get(f'/api/v4/projects/{pid}/learnings/export', params={'source_id': source_id})
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert '读取版本字段再处理' in archive.read('SKILL.md').decode()
        assert json.loads(archive.read('source.json'))['project_id'] == pid
    uploaded = client.post(f"/api/v4/agents/{a['id']}/skills", headers=headers, files={'file': ('learning.zip', exported.content, 'application/zip')})
    assert uploaded.status_code == 201, uploaded.text
