import io
import json
import time
import zipfile

from factory.control.agents import AgentStore
from factory.control.providers import ProviderResult
from tests.test_control_app import login
from tests.test_workbench_app import app_env, project
import time


def _zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('SKILL.md', 'convert customer format')
        z.writestr('refs/example.txt', 'reference')
    return buf.getvalue()


def test_agent_version_is_frozen_and_skill_is_data_only(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    created = client.post('/api/v4/agents', json={
        'name': '格式维护员', 'purpose': '维护转换规则',
        'model_settings': {'default': {'provider': 'codex', 'model': 'test'}},
    }, headers=headers)
    assert created.status_code == 201, created.text
    agent = created.json()
    upload = client.post(f"/api/v4/agents/{agent['id']}/skills",
                         files={'file': ('skill.zip', _zip(), 'application/zip')}, headers=headers)
    assert upload.status_code == 201, upload.text
    assert upload.json()['files'][0]['path'] == 'SKILL.md'
    # The raw archive is retained as bytes and never imported as executable code.
    with store.connect() as db:
        assert db.execute('SELECT length(content) FROM skill_assets').fetchone()[0] > 0
    conversation = client.post(f"/api/v4/agents/{agent['id']}/conversations",
                               json={'mode': 'do'}, headers=headers)
    assert conversation.status_code == 201
    cid = conversation.json()['id']
    first = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '先整理输入格式'}, headers=headers)
    assert first.status_code == 201 and first.json()['status'] == 'pending' and first.json()['run'] is None

    p = project(client, repo, headers)
    bound = client.post(f'/api/v4/conversations/{cid}/project', json={'project_id': p['id']}, headers=headers)
    assert bound.status_code == 200
    second = client.post(f'/api/v4/conversations/{cid}/messages', json={'content': '修改 greeting.txt'}, headers=headers)
    assert second.status_code == 201 and second.json()['run']['agent_snapshot']['version'] == 1
    rid = second.json()['run']['id']
    deadline = time.time() + 5
    while time.time() < deadline and store.get(rid)['status'] in {'received','planning','queued','running','verifying'}:
        time.sleep(.02)
    frozen = store.get(rid)
    assert frozen['agent_id'] == agent['id'] and frozen['runtime_configuration']['profiles']['planner']['model'] == 'test'


def test_maintenance_returns_pending_and_can_be_cancelled(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = client.post('/api/v4/agents', json={
        'name': '维护员', 'model_settings': {'maintenance': {'provider': 'codex', 'model': 'test'}},
    }, headers=headers).json()
    c = client.post(f"/api/v4/agents/{agent['id']}/conversations", json={'mode': 'maintain'}, headers=headers).json()
    original = service.runner.run
    def slow(request, emit, cancel=None):
        while cancel is not None and not cancel.is_set():
            time.sleep(0.005)
        return ProviderResult(json.dumps({'patch': {'instructions': 'x'}, 'conflicts': []}))
    monkeypatch.setattr(service.runner, 'run', slow)
    response = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': '读取这个 Skill'}, headers=headers)
    assert response.status_code == 201 and response.json()['status'] == 'pending'
    job_id = response.json()['job_id']
    cancelled = client.post(f'/api/v4/maintenance-jobs/{job_id}/cancel', headers=headers)
    assert cancelled.status_code == 200
    # The job endpoint is durable enough to distinguish cancellation request
    # from a still-running provider process.
    assert client.get(f'/api/v4/maintenance-jobs/{job_id}', headers=headers).status_code == 200
    monkeypatch.setattr(service.runner, 'run', original)


def test_maintenance_freezes_revision_and_clears_conflicts(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    agent = client.post('/api/v4/agents', json={'name': '规则维护', 'model_settings': {'maintenance': {'provider': 'codex', 'model': 'test'}}}, headers=headers).json()
    c = client.post(f"/api/v4/agents/{agent['id']}/conversations", json={'mode': 'maintain'}, headers=headers).json()
    prompts=[]; responses=iter([
        {'patch': {'instructions': 'first'}, 'explanation': ['x'], 'conflicts': ['旧规则冲突']},
        {'patch': {'instructions': 'second'}, 'explanation': ['y'], 'conflicts': []},
    ])
    def model(request, emit, cancel=None):
        prompts.append(request.prompt)
        return ProviderResult(json.dumps(next(responses)), cost_usd=.01, tokens_in=3, tokens_out=4)
    monkeypatch.setattr(service.runner, 'run', model)
    first=client.post(f"/api/v4/conversations/{c['id']}/messages",json={'content':'请读取技能并整理'},headers=headers)
    assert first.status_code==201
    deadline=time.time()+3
    while time.time()<deadline and service.maintenance_status(first.json()['job_id']).get('status') not in ('completed','failed'): time.sleep(.01)
    draft=client.get(f"/api/v4/agents/{agent['id']}/draft",headers=headers).json(); assert draft['revision']==1 and draft['conflicts']==['旧规则冲突']
    second=client.post(f"/api/v4/conversations/{c['id']}/messages",json={'content':'解决冲突并按新版保存'},headers=headers)
    deadline=time.time()+3
    while time.time()<deadline and service.maintenance_status(second.json()['job_id']).get('status') not in ('completed','failed'): time.sleep(.01)
    draft=client.get(f"/api/v4/agents/{agent['id']}/draft",headers=headers).json(); assert draft['revision']==2 and draft['conflicts']==[]
    assert 'Allowed patch keys' in prompts[0] and 'CONVERSATION' in prompts[1]
