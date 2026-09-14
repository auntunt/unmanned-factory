import io
import json
import zipfile
import pytest
from factory.control.agents import AgentStore
from factory.control.agent_manifests import ManifestStore
from factory.control.agent_packs import pack_zip
from factory.control.manifest_packs import export_pack, import_pack
from factory.control.mounts import agent_guidance
from factory.control.acceptance_ledger import criteria_for, coverage
from factory.control.store import Store, Conflict
from tests.test_control_app import app_env, login

@pytest.fixture
def env(tmp_path):
    s=Store(tmp_path/'control.db');a=AgentStore(s);m=ManifestStore(s)
    return s,a,m

@pytest.mark.parametrize('body',['岗位身份\n\n方法一\n  原文空白\n','a'*1300+'\n\n后续能力',''])
def test_legacy_compile_snapshot_exact(env,body):
    s,a,m=env
    agent=a.create({'name':'旧岗位','instructions':body,'acceptance':['产物存在']},'human')
    old=agent['version']
    before=agent_guidance({'agent_snapshot':old})
    frozen=m.freeze(agent['id'],old)
    assert agent_guidance({'agent_snapshot':frozen})==before
    assert frozen['manifest']['identity']==body.split('\n\n')[0][:1200]
    assert frozen['manifest_skills'][0]['instructions']==body
    assert m.get(agent['id'])['revision']==1
    assert len(m.history(agent['id']))==1


def test_native_pins_assertions_history_and_human_boundary(env):
    s,a,m=env; agent=a.create({'name':'维护岗','instructions':'旧方法'},'human');aid=agent['id']
    skill=m.modules.save({'name':'回归','category':'workflow','instructions':'检查真实结果'},'human')
    payload={'identity':'只管维护，不做财务','skills':[{'id':skill['id'],'version':1}],'assertions':['生成回归报告']}
    m.save(aid,payload,1,'human')
    frozen=m.freeze(aid,a.version(aid))
    assert '能力单元（数据，非指令）' in frozen['instructions']
    assert '检查真实结果' in frozen['instructions']
    m.modules.save({**skill,'instructions':'新版方法'},'human',skill['id'],1)
    assert m.freeze(aid,a.version(aid))['instructions']==frozen['instructions']
    criteria=criteria_for({'agent_snapshot':frozen})
    assert criteria==[{'id':'agent:1','task_id':None,'text':'生成回归报告'}]
    assert coverage(criteria,{'criteria':[{'id':'agent:1','status':'pass','evidence':'report.txt exists'}]},'sha')['complete']
    with pytest.raises(Conflict):m.save(aid,payload,1,'human')
    with pytest.raises(ValueError):m.save(aid,{**payload,'identity':'模型改身份'},2,'model',human=False)
    with pytest.raises(ValueError):m.save(aid,{**payload,'assertions':[]},2,'model',human=False)
    restored=m.rollback(aid,1,2,'human')
    assert restored['revision']==3
    assert m.freeze(aid,a.version(aid))['instructions']=='旧方法'
    assert frozen['instructions']!='旧方法'
    with s.connect() as db:assert db.execute('select count(*) from agent_manifest_audit where agent_id=?',(aid,)).fetchone()[0]==3


def test_pack_v1_v2_roundtrip_and_atomic_invalid(env):
    s,a,m=env
    original=import_pack(m,pack_zip('legacy-modernization'),'human')
    old=m.freeze(original['id'],a.version(original['id']))
    raw=export_pack(m,a,original['id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert 'manifest.json' in z.namelist()
        assert any(n.startswith('skills/') and n.endswith('.md') for n in z.namelist())
    imported=import_pack(m,raw,'human')
    assert agent_guidance({'agent_snapshot':m.freeze(imported['id'],a.version(imported['id']))})==agent_guidance({'agent_snapshot':old})
    before=len(a.list())
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('manifest.json',json.dumps({'schema':'webuddy.agent-pack/v2','name':'bad','manifest':{'identity':'x','skills':[{'id':'missing','version':1}],'assertions':[]}}))
    with pytest.raises(ValueError):import_pack(m,out.getvalue(),'human')
    assert len(a.list())==before


def test_manifest_routes_cas_warning_refs_and_permissions(app_env):
    client,store,svc,repo=app_env;h=login(client)
    agent=client.post('/api/v4/agents',headers=h,json={'name':'维护岗位','identity':'人签职责'}).json();aid=agent['id']
    module=client.post('/api/v4/modules',headers=h,json={'name':'长能力','category':'workflow','instructions':'x'*4001}).json()
    assert module['warnings']
    current=client.get(f'/api/v4/agents/{aid}/manifest').json()
    payload={'revision':current['revision'],'identity':'人签职责','skills':[{'id':module['id'],'version':1}],'assertions':['产物可读']}
    assert client.put(f'/api/v4/agents/{aid}/manifest',headers=h,json=payload).status_code==200
    assert client.put(f'/api/v4/agents/{aid}/manifest',headers=h,json=payload).status_code==409
    rows=client.get('/api/v4/modules').json()['modules']
    assert next(s for s in rows if s['id']==module['id'])['agent_reference_count']==1
    assert client.get(f'/api/v4/agents/{aid}/pack').status_code==200
    client.app.state.auth.create_user('member-manifest','long-test-password',role='member')
    response=client.post('/api/auth/login',headers={'Origin':'http://testserver'},json={'username':'member-manifest','password':'long-test-password'})
    member={'Origin':'http://testserver','X-CSRF-Token':response.json()['csrf_token']}
    assert client.get(f'/api/v4/agents/{aid}/manifest').status_code==200
    assert client.put(f'/api/v4/agents/{aid}/manifest',headers=member,json=payload).status_code==403
