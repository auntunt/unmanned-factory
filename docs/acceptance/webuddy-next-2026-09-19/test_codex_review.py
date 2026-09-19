import io,json,threading,zipfile
from dataclasses import asdict
from tests.test_admin_config_conversation import env, _login, _member, _wait_job
from factory.control.agents import AgentStore
from factory.control.providers import ProviderResult

MARKER='BODY_ONLY_COMPANY_RULE_83F7'
def skill_zip():
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('SKILL.md','---\nname: company-rule\ndescription: innocuous metadata\n---\nApply the private rule '+MARKER+' when answering.')
    return b.getvalue()
def setup_session(client,store,headers):
    agents=AgentStore(store)
    aid=client.post('/api/v4/agents',headers=headers,json={'name':'Review helper','purpose':'answer'}).json()['id']
    cid=client.post(f'/api/v4/agents/{aid}/conversations',headers=headers,json={'mode':'do'}).json()['id']
    r=client.post(f'/api/v4/sessions/{cid}/skills',headers=headers,files={'file':('skill.zip',skill_zip(),'application/zip')})
    assert r.status_code==201,r.text
    return cid,r.json()

def test_session_skill_body_reaches_chat_runner(env,monkeypatch):
    client,store,service=env;h=_login(client);cid,skill=setup_session(client,store,h)
    seen=[]
    def run(req,emit,cancel):seen.append(req);return ProviderResult('done')
    monkeypatch.setattr(service.runner,'run',run)
    r=client.post(f'/api/v4/conversations/{cid}/messages',headers=h,json={'content':'Use my attached company rule.'})
    assert r.status_code==201,r.text
    assert _wait_job(service,r.json()['job_id'])['status']=='completed'
    assert seen
    assert MARKER in json.dumps(asdict(seen[0]),ensure_ascii=False), 'Imported SKILL.md body never reaches the runner prompt or reference mount'

def test_member_cannot_read_another_users_session_skills(env):
    client,store,service=env;h=_login(client);cid,skill=setup_session(client,store,h)
    mh=_member(client,'unrelated')
    r=client.get(f'/api/v4/sessions/{cid}/skills',headers=mh)
    assert r.status_code in (403,404),f'Unrelated member reads session metadata: {r.status_code} {r.text}'

def test_completed_admin_answer_clears_pending(env,monkeypatch):
    client,store,service=env;h=_login(client);gate=threading.Event()
    def run(req,emit,cancel):gate.wait(5);return ProviderResult('Config answer completed')
    monkeypatch.setattr(service.runner,'run',run)
    cid=client.post('/api/v4/admin-config/conversations',headers=h,json={}).json()['id']
    r=client.post(f'/api/v4/admin-config/conversations/{cid}/messages',headers=h,json={'content':'Read configuration'})
    assert r.status_code==201,r.text
    gate.set();assert _wait_job(service,r.json()['job_id'])['status']=='completed'
    conv=client.get(f'/api/v4/admin-config/conversations/{cid}',headers=h).json()
    assert any(m.get('status')=='completed' for m in conv['messages'])
    assert not any(m.get('status') in ('pending','running') for m in conv['messages']), 'Completed job leaves permanent pending message, frontend never stops polling'
