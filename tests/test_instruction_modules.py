import pytest
from factory.control.modules import ModuleStore, module_prompt
from tests.test_control_app import login, project
from tests.test_workbench_app import app_env
from tests.test_continuous_service import prepared


def test_modules_are_pinned_and_frozen_at_task_start(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client); p = project(client, repo, headers)
    monkeypatch.setattr(service, '_submit', lambda *args: None)
    body = dict(name='业务知识', category='knowledge', description='真实格式', instructions='VERSION_ONE_MARKER')
    first = client.post('/api/v4/modules', headers=headers, json=body).json()
    url = f"/api/v4/projects/{p['id']}/modules"
    refs = [{'id':first['id'],'version':1}, {'id':'builtin-clean-ui','version':1}]
    assert client.put(url, headers=headers, json={'expected_revision':0,'modules':refs}).status_code == 200
    r = client.post('/api/v2/runs', headers=headers, json={'project_id':p['id'],'request':'构建应用'}).json()
    frozen = store.get(r['id'])
    assert 'VERSION_ONE_MARKER' in module_prompt(frozen)
    assert len(frozen['module_snapshot']) == 2
    assert client.put('/api/v4/modules/'+first['id'], headers=headers, json={**body,'instructions':'VERSION_TWO_MARKER','expected_revision':1}).status_code == 200
    assert client.get(url).json()['modules'][0]['version'] == 1
    assert client.put(url, headers=headers, json={'expected_revision':1,'modules':[]}).status_code == 200
    assert len(store.get(r['id'])['module_snapshot']) == 2
    service.start_plan(r['id'])
    assert len(store.get(r['id'])['module_snapshot']) == 2


def test_style_conflicts_revision_and_auth(app_env):
    client, store, svc, repo = app_env
    assert client.get('/api/v4/modules').status_code == 401
    headers=login(client); p=project(client,repo,headers)
    url=f"/api/v4/projects/{p['id']}/modules"
    styles=[{'id':m,'version':1} for m in ['builtin-clean-ui','builtin-warm-ui']]
    assert client.put(url,headers=headers,json={'expected_revision':0,'modules':styles}).status_code==400
    assert client.get(url).json()['revision']==0
    assert client.put(url,headers=headers,json={'expected_revision':0,'modules':styles[:1]}).status_code==200
    assert client.put(url,headers=headers,json={'expected_revision':0,'modules':[]}).status_code==409
    assert client.put(url,json={'expected_revision':1,'modules':[]}).status_code==403
    assert client.put(url,headers=headers,json={'expected_revision':1,'modules':[{'id':'missing','version':1}]}).status_code==400
    assert client.get(url).json()['revision']==1


def test_continuous_executor_receives_frozen_modules(app_env, monkeypatch):
    store, svc, p, rid, submitted=prepared(app_env, monkeypatch)
    m=ModuleStore(store).list()[0]
    store.update(rid, {'module_snapshot':[m]})
    svc._plan(rid)
    prompts=[]
    def execute(**kwargs):
        prompts.append(kwargs['plan']['tasks'][0]['prompt'])
        return {'worktree':p['workspace'],'tasks':[{'id':'coding','status':'completed'}], 'checks':[], 'known_cost_usd':0}
    svc.continuous_execute=execute
    monkeypatch.setattr(svc,'_independent_verify',lambda *args:None)
    svc._run(rid)
    assert len(prompts)==1
    assert m['instructions'] in prompts[0]
    assert prompts[0].count(m['instructions'])==1
