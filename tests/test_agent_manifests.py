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


def test_human_maintenance_apply_becomes_skill_without_rewriting_identity(env):
    s,a,m=env;aid=a.create({'name':'长期岗位','instructions':'旧资料'},'human')['id']
    m.save(aid,{'identity':'人签职责不变','skills':[],'assertions':['结果可复核']},1,'human')
    draft=a.save_draft(aid,{'instructions':'按真实日志复现'},0)
    a.apply(aid,draft['revision'],actor='user-1')
    frozen=m.freeze(aid,a.version(aid))
    assert frozen['manifest']['identity']=='人签职责不变'
    assert frozen['acceptance']==['结果可复核']
    assert '按真实日志复现' in frozen['instructions']
    assert frozen['manifest_skills'][0]['source']['type']=='maintenance'
    assert m.get(aid)['revision']==3


def test_compiled_prompt_is_rejected_instead_of_silently_truncated(env):
    s,a,m=env;aid=a.create({'name':'大岗位'},'human')['id']
    refs=[]
    for i in range(7):
        skill=m.modules.save({'name':str(i),'category':'workflow','instructions':'"'*14000},'human')
        refs.append({'id':skill['id'],'version':1})
    with pytest.raises(ValueError,match='不能静默截断'):
        m.save(aid,{'identity':'范围','skills':refs,'assertions':[]},1,'human')
    assert m.get(aid)['revision']==1


def test_large_pack_upload_uses_bounded_authenticated_stream(app_env):
    client,s,svc,repo=app_env;h=login(client)
    # A real v2 pack with a >1 MiB asset must roundtrip through the HTTP importer.
    aid=svc.agents.create({'name':'带资料的岗位'},'human')['id']
    import random
    import uuid
    from factory.control.agents import inspect_skill
    from factory.control.store import now
    content=io.BytesIO()
    with zipfile.ZipFile(content,'w') as z:z.writestr('reference.txt',random.Random(1).randbytes(1100000).hex()[:1100000])
    raw=content.getvalue();sid=uuid.uuid4().hex
    meta={**inspect_skill(raw),'id':sid,'agent_id':aid,'filename':'source.zip','source':'upload','created_at':now()}
    with s.connect() as db:db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)',(sid,aid,json.dumps(meta),raw,now()))
    d=svc.agents.save_draft(aid,{'skill_ids':[sid]},0);svc.agents.apply(aid,d['revision'])
    pack=export_pack(svc.agent_manifests,svc.agents,aid)
    # ZIP compression can shrink textual fixtures: store outer entries verbatim.
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(pack)) as old,zipfile.ZipFile(out,'w') as z:
        for name in old.namelist():z.writestr(name,old.read(name))
    pack=out.getvalue();assert len(pack)>1048576
    response=client.post('/api/v4/agent-packs/import',headers=h,files={'file':('pack.zip',pack,'application/zip')})
    assert response.status_code==201,response.text
    assert client.post('/api/v4/agent-packs/import',headers={'Origin':'http://testserver'},files={'file':('pack.zip',pack)}).status_code==403
    client.cookies.clear()
    assert client.post('/api/v4/agent-packs/import',headers={'Origin':'http://testserver'},files={'file':('pack.zip',pack)}).status_code==401


def test_external_zip_wrong_entry_explains_adaptation_without_creating_agent(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('reverse-skill/SKILL.md', '---\nname: reverse-skill\n---\nRead and classify only.')
    before = len(service.agents.list())
    response = client.post('/api/v4/agent-packs/import', headers=headers,
        files={'file': ('reverse-skill.zip', archive.getvalue(), 'application/zip')})
    assert response.status_code == 400
    assert '导入外部 skill 包' in response.json()['detail']
    assert '人签' in response.json()['detail']
    assert len(service.agents.list()) == before
