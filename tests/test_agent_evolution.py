import json
from datetime import datetime,timezone
import pytest
from factory.control.agent_evolution import EvolutionStore
from factory.control.agent_manifests import ManifestStore
from factory.control.agents import AgentStore
from factory.control.operations_automation import OperationsAutomation
from factory.control.project_assistants import ProjectAssistants
from factory.control.acceptance_ledger import coverage
from factory.control.store import Store,Conflict
from tests.test_control_app import app_env,login,project

@pytest.fixture
def env(tmp_path):
    s=Store(tmp_path/'control.db');a=AgentStore(s);m=ManifestStore(s);o=OperationsAutomation(s);e=EvolutionStore(s,m)
    p=s.add_project({'name':'project','repository':'owner/repo','workspace':str(tmp_path),'base_branch':'main','checks':[]})
    aid=a.create({'name':'维护岗位','instructions':'人签身份\n\n方法','acceptance':['报告存在']},'human')['id']
    helper=ProjectAssistants(s);helper.bind(p['id'],aid,0,'human')
    return s,a,m,o,e,p,aid

def observed(env,index=0,refs=None,status='ready_for_review',ledger=True):
    s,a,m,o,e,p,aid=env
    run=s.create_run(p['id'],'验证任务')[0]
    at=f'2026-09-14T10:{index:02}:00+00:00'
    artifacts={'verification':{'verdict':'pass'}}
    if ledger:artifacts['acceptance_ledger']={'items':[{'id':'agent:1','status':'pass','evidence':'report.txt verified','skill_refs':refs or []}]}
    return s.update(run['id'],{'status':status,'created_at':at,'agent_id':aid,'agent_snapshot':m.freeze(aid,a.version(aid)),'artifacts':artifacts})

def capability(env,run):
    return env[4].capabilities.create({'name':'验证方法','description':'从任务沉淀','category':'engineering','instructions':'运行回归检查',
          'input_description':'输入','output_description':'报告','status':'draft','acceptance':['有报告']},source_run_id=run['id'])

def test_evidence_required_and_auto_type_boundary(env):
    s,a,m,o,e,p,aid=env;run=observed(env)
    with pytest.raises(ValueError):e.create(aid,p['id'],'add_assertion',{'text':'x'},{'run_ids':[],'explanation':'无证据'},'human')
    with pytest.raises(ValueError):e.create(aid,p['id'],'add_assertion',{'text':'x'},{'run_ids':['missing'],'explanation':'不存在'},'human')
    ev={'run_ids':[run['id']],'explanation':'真实验收证据'}
    proposal=e.create(aid,p['id'],'add_assertion',{'text':'新增核对'},ev,'human')
    e.configure(p['id'],0,True,True,'human')
    with s.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError):e._decide(db,proposal,True,1,'auto',automatic=True)
    before=m.get(aid)
    assert e.decide(proposal['id'],True,1,'human')['status']=='approved'
    assert m.get(aid)['identity']==before['identity']
    assert '新增核对' in m.get(aid)['assertions']
    with pytest.raises(Conflict):e.decide(proposal['id'],True,1,'human')


def test_promotion_draft_provenance_idempotency_auto_and_notification_failure(env,monkeypatch):
    s,a,m,o,e,p,aid=env;run=observed(env);c=capability(env,run)
    pending=e.promote(c['id'],1,aid,'human')
    assert pending['status']=='pending' and pending['evidence']['run_ids']==[run['id']]
    assert e.promote(c['id'],1,aid,'human')['id']==pending['id']
    skill=m.modules.list()[-1];assert skill['status']=='draft' and skill['source']['run_id']==run['id']
    e.decide(pending['id'],False,1,'human')
    e.configure(p['id'],0,True,True,'human');c2=capability(env,run)
    approved=e.promote(c2['id'],1,aid,'human');assert approved['status']=='approved' and approved['automatic']
    assert m.get(aid)['revision']==approved['result_revision']
    o.configure(0,False,'https://open.feishu.cn/open-apis/bot/v2/hook/fake-key')
    sent=[]
    def fail(url,text):sent.append(text);raise OSError('secret-url')
    monkeypatch.setattr(o,'send',fail)
    o.tick();o.tick()
    assert len(sent)==1 and '/agents/'+aid in sent[0]
    assert e.list(aid)[0]['status']=='approved'
    e.configure(p['id'],1,True,False,'human');e.promote(capability(env,run)['id'],1,aid,'human');o.tick()
    assert len(sent)==1


def test_weekly_five_unused_and_evidence_breaks_streak(env):
    s,a,m,o,e,p,aid=env
    stamp=datetime(2026,9,20,tzinfo=timezone.utc).timestamp()
    for i in range(4):observed(env,i)
    assert e.weekly(stamp)==[]
    observed(env,4)
    assert e.weekly(stamp)==[]  # one persistent aggregate per week
    proposals=e.weekly(stamp+7*86400)
    assert len(proposals)==1 and proposals[0]['kind']=='remove_skill' and proposals[0]['status']=='pending'
    assert len(proposals[0]['evidence']['run_ids'])==5
    assert e.weekly(stamp+14*86400)[0]['id']==proposals[0]['id']
    ref=m.get(aid)['skills'][0];observed(env,5,[ref])
    assert e.weekly(stamp+21*86400)==[]
    with s.connect() as db:assert db.execute('select count(*) from evolution_weekly').fetchone()[0]==4


def test_auto_remove_no_ledger_no_proposal_and_unrelated_job(env):
    s,a,m,o,e,p,aid=env;e.configure(p['id'],0,True,True,'human')
    for i in range(5):observed(env,i,ledger=i!=0)
    stamp=datetime(2026,9,20,tzinfo=timezone.utc).timestamp()
    assert e.weekly(stamp)==[]
    observed(env,5)
    removed=e.weekly(stamp+7*86400)[0]
    assert removed['status']=='approved' and removed['automatic']
    assert m.get(aid)['skills']==[] and m.get(aid)['identity']=='人签身份'
    other=a.create({'name':'其他岗位','instructions':'只读'},'human')['id']
    run=observed(env,6);c=capability(env,run)
    assert e.promote(c['id'],1,other,'human')['status']=='pending'


def test_ledger_rejects_unmounted_skill_citations():
    criteria=[{'id':'a','text':'works'}];ref={'id':'s','version':1}
    row={'id':'a','status':'pass','evidence':'actual output','skill_refs':[ref]}
    ledger=coverage(criteria,{'criteria':[row]},'commit',skills=[ref])
    assert ledger['complete'] and ledger['items'][0]['skill_refs']==[ref]
    assert not coverage(criteria,{'criteria':[row]},'commit',skills=[])['complete']
    assert not coverage(criteria,{'criteria':[{**row,'skill_refs':[{'id':'s','version':True}]}]},'commit',skills=[ref])['complete']


def test_stale_manifest_and_upgrade_require_human(env):
    s,a,m,o,e,p,aid=env;run=observed(env);ref=m.get(aid)['skills'][0]
    skill=m.resolve(m.get(aid))[0]
    m.modules.save({**skill,'instructions':'升级后的方法'},'human',ref['id'],1)
    ev={'run_ids':[run['id']],'explanation':'旧版证据提示改进'}
    proposal=e.create(aid,p['id'],'upgrade_skill',{'id':ref['id'],'version':2},ev,'human')
    e.configure(p['id'],0,True,True,'human')
    with s.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError):e._decide(db,proposal,True,1,'auto',automatic=True)
    current=m.get(aid);m.save(aid,{k:current[k] for k in ('identity','skills','assertions')},current['revision'],'human')
    with pytest.raises(Conflict):e.decide(proposal['id'],True,1,'human')
    assert e.decide(proposal['id'],False,1,'human')['status']=='rejected'


def test_evolution_api_refuses_missing_evidence_and_source_spoof(app_env):
    client,s,svc,repo=app_env;h=login(client);p=project(client,repo,h)
    aid=client.post('/api/v4/agents',headers=h,json={'name':'岗位'}).json()['id']
    body={'project_id':p['id'],'kind':'add_assertion','change':{'text':'x'},'evidence':{}}
    assert client.post(f'/api/v4/agents/{aid}/evolution',headers=h,json=body).status_code==400
    assert client.post(f'/api/v4/agents/{aid}/evolution',headers=h,json={**body,'source':'capability'}).status_code==422
    policy={'revision':0,'auto_evolve':True,'notifications':False}
    assert client.put(f"/api/v4/projects/{p['id']}/evolution-policy",headers=h,json=policy).status_code==200
    assert client.put(f"/api/v4/projects/{p['id']}/evolution-policy",headers=h,json=policy).status_code==409
    assert client.get(f'/api/v4/agents/{aid}/evolution').json()=={'proposals':[]}
