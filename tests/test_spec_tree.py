import json
from pathlib import Path
import subprocess
import threading

import pytest

from factory.control import spec_tree as spec
from factory.control.acceptance_ledger import coverage, criteria_for
from factory.control.execution import ExecutionError
from factory.control.modules import ModuleStore, module_prompt
from factory.control.providers import ProviderResult
from tests.test_control_app import app_env, login, project
from tests.review_helpers import passing_review


def command(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def commit(root, message):
    command(root, 'add', '.')
    command(root, 'commit', '-qm', message)
    return command(root, 'rev-parse', 'HEAD')


def document(code='app.py', raw='人签要求'):
    return f'---\ntitle: 应用\nstatus: active\ndesc: 应用边界\ncode:\n  - {code}\nrelated:\n  - README.md\n---\n## raw source\n{raw}\n\n## expanded spec\n当前行为\n'


@pytest.fixture
def repo(tmp_path):
    root = tmp_path/'repo';root.mkdir()
    command(root,'init','-q','-b','main');command(root,'config','user.name','Test');command(root,'config','user.email','test@example.com')
    (root/'app.py').write_text('def target():\n    return 1\n\ndef other():\n    return 2\n')
    node=root/'.spec/app/spec.md';node.parent.mkdir(parents=True);node.write_text(document())
    commit(root,'initial')
    return root


def test_parse_upstream_lists_parts_and_invalid(repo):
    nested=repo/'.spec/app/child/spec.md';nested.parent.mkdir();nested.write_text(document('app.py#target'))
    bad=repo/'.spec/app/bad/spec.md';bad.parent.mkdir();bad.write_text('---\ntitle: bad\ncode: [broken]\ninvalid line\n---\ntext')
    nodes=spec.tree(repo)
    child=next(n for n in nodes if n['path'].endswith('/child/spec.md'))
    assert child['parent']=='.spec/app/spec.md'
    assert child['code']==[{'entry':'app.py#target','path':'app.py','symbol':'target'}]
    assert child['raw_source']=='人签要求' and child['expanded']=='当前行为'
    assert next(n for n in nodes if '/bad/' in n['path'])['status']=='invalid'
    assert spec.parse('---\nnot closed', '.spec/b/spec.md')['errors']
    fenced=spec.parse(document().replace('当前行为','```\n## raw source\n```'),'.spec/a/spec.md')
    assert '## raw source' in fenced['expanded']


def test_same_commit_none_and_code_only_file(repo):
    (repo/'app.py').write_text('def target():\n    return 3\n')
    (repo/'.spec/app/spec.md').write_text(document(raw='updated'))
    sha=commit(repo,'spec and code')
    assert spec.drift(repo,sha)['.spec/app/spec.md']['level']=='none'
    (repo/'app.py').write_text('def target():\n    return 4\n')
    newer=commit(repo,'code only')
    result=spec.drift(repo,newer)['.spec/app/spec.md']
    assert result['level']=='file' and result['commits'][0]['sha']==newer
    assert len(result['history'])==2
    assert spec.drift(repo,sha)['.spec/app/spec.md']['level']=='none'


@pytest.mark.parametrize('symbol,change,level,unresolved',[
    ('target','return 1','anchored',False),('target','return 2','file',False),('missing','return 1','file',True)])
def test_anchored_hunks_and_fallback(repo,symbol,change,level,unresolved):
    (repo/'.spec/app/spec.md').write_text(document('app.py#'+symbol));commit(repo,'anchor')
    p=repo/'app.py';p.write_text(p.read_text().replace(change,'return 9'));commit(repo,'change')
    result=spec.drift(repo)['.spec/app/spec.md']
    assert result['level']==level
    assert any('symbol_unresolved' in r for r in result['reasons'])==unresolved


def test_deleted_symbol_is_anchored(repo):
    (repo/'.spec/app/spec.md').write_text(document('app.py#target'));commit(repo,'anchor')
    (repo/'app.py').write_text('def other():\n    return 2\n');commit(repo,'delete target')
    assert spec.drift(repo)['.spec/app/spec.md']['level']=='anchored'


def test_absent_disabled_git_failure_and_ledger(repo,monkeypatch):
    assert spec.evidence({},repo,'HEAD')==[]
    command(repo,'rm','-r','.spec');commit(repo,'remove specs')
    assert spec.evidence({'spec_tree_enabled':True},repo,'HEAD')==[]
    monkeypatch.setattr(spec,'drift',lambda *a: (_ for _ in ()).throw(spec.SpecError('broken git')))
    items=spec.evidence({'spec_tree_enabled':True},repo,'HEAD')
    assert items[0]['status']=='unverified' and 'broken git' in items[0]['evidence']
    ledger=coverage([{'id':'a','text':'works'}],{'criteria':[{'id':'a','status':'pass','evidence':'yes'}]},'sha')
    result=spec.apply_evidence(ledger,{'verdict':'pass','reason':'ok'},items)
    assert result['verdict']=='unverified' and ledger['counts']=={'pass':1,'fail':0,'unverified':1}


def test_paths_symlinks_and_task_context(repo,tmp_path):
    with pytest.raises(spec.SpecError):spec.code_entry('../private#x')
    (repo/'.spec/app/link').symlink_to(tmp_path,target_is_directory=True)
    assert len(spec.tree(repo))==1
    plan={'tasks':[{'paths':['app.py'],'prompt':'edit'}]}
    (repo/'.spec/app/spec.md').write_text(document()+('x'*4000))
    spec.enrich_tasks({'workspace':str(repo),'spec_tree_enabled':True},plan)
    task=plan['tasks'][0]
    assert '.spec/app/spec.md' in task['paths'] and '已截断' in task['prompt']
    assert len(task['prompt'].split('reference data):\n')[1])<=2000


def test_settings_init_audit_no_hooks_and_bootstrap_idempotent(app_env,monkeypatch):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    hooks=root/'.git/hooks/pre-commit';hooks.write_text('#!/bin/sh\nexit 55\n');hooks.chmod(0o755)
    url=f"/api/v2/projects/{p['id']}/spec-tree"
    assert client.get(url).json()['nodes']==[]
    result=client.put(url+'/settings',json={'enabled':True,'revision':p['revision']},headers=headers)
    assert result.status_code==200,result.text
    assert command(root,'status','--porcelain')==''
    assert json.loads((root/'.spec/spexcode.json').read_text())=={'dashboard':{'title':'Sample'}}
    assert (root/'.spec/sample/spec.md').read_text().startswith('---\ntitle: sample\nstatus: active\nhue: 45')
    assert not (root/'CLAUDE.md').exists() and not (root/'AGENTS.md').exists()
    assert hooks.read_text().endswith('exit 55\n')
    assert store.project_audit(p['id'])[-1]['data']['spec_tree_enabled'] is True
    assert client.put(url+'/settings',json={'enabled':False,'revision':p['revision']},headers=headers).status_code==409
    assert client.get(url+'/node',params={'path':'../private'}).status_code==400
    assert client.get(url+'/node',params={'path':'.spec/missing/spec.md'}).status_code==404
    assert client.get(url+'/node',params={'path':'.spec/sample/spec.md'}).json()['history']
    calls=[];monkeypatch.setattr(service,'start_plan',calls.append)
    first=client.post(url+'/generate',json={'idempotency_key':'spec-request-01'},headers=headers)
    second=client.post(url+'/generate',json={'idempotency_key':'spec-request-01'},headers=headers)
    assert first.status_code==201 and first.json()['id']==second.json()['id']
    assert calls==[first.json()['id']]
    run=store.get(calls[0]);assert run['source']['spec_bootstrap'] and 'raw source 留空' in run['request']
    frozen=ModuleStore(store).freeze(run)
    assert 'SPEC TREE' in module_prompt(frozen)


@pytest.mark.parametrize('anchored',[False,True])
def test_verification_snapshot_uses_source_git_and_mechanical_ledger(app_env,monkeypatch,anchored):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    p=store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'owner')
    node=root/'.spec/sample/spec.md';node.write_text(document('app.py#target' if anchored else 'app.py'))
    (root/'app.py').write_text('def target():\n    return 1\n');commit(root,'baseline')
    (root/'app.py').write_text('def target():\n    return 2\n');sha=commit(root,'drift')
    run=store.create_run(p['id'],'verify')[0];service.cancels[run['id']]=threading.Event()
    def review(req,*a,**kw):
        assert not (Path(req.workspace)/'.git').exists()
        assert 'SPEC TREE' in req.prompt
        return ProviderResult(passing_review(req,'observed'),cost_usd=.01)
    monkeypatch.setattr(service.runner,'run',review)
    artifacts={}
    with pytest.raises(ExecutionError):service._independent_verify(run['id'],run,p,service.runtime_settings.get(),artifacts)
    ledger=artifacts['acceptance_ledger']
    assert ledger['commit']==sha
    assert ledger['counts']['fail' if anchored else 'unverified']==1
    assert artifacts['verification']['verdict']==('fail' if anchored else 'unverified')
    assert next(i for i in ledger['items'] if i.get('spec_path'))['spec_path']=='.spec/sample/spec.md'


def test_spec_membership_and_admin_only_writes(app_env):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'owner')
    user=client.app.state.auth.create_user('spec-member','very-long-password',role='member')
    reply=client.post('/api/auth/login',headers={'Origin':'http://testserver'},json={'username':'spec-member','password':'very-long-password'})
    h={'Origin':'http://testserver','X-CSRF-Token':reply.json()['csrf_token']}
    url=f"/api/v2/projects/{p['id']}/spec-tree"
    assert client.get(url).status_code==403
    service.governance.assign(user['id'],[p['id']],1)
    assert client.get(url).status_code==200
    assert client.get(url+'/node',params={'path':'.spec/sample/spec.md'}).status_code==200
    assert client.put(url+'/settings',headers=h,json={'enabled':False,'revision':2}).status_code==403
    assert client.post(url+'/generate',headers=h,json={'idempotency_key':'spec-request-02'}).status_code==403


def test_git_failure_downgrades_real_verifier(app_env,monkeypatch):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    p=store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'owner')
    run=store.create_run(p['id'],'verify')[0];service.cancels[run['id']]=threading.Event()
    monkeypatch.setattr(spec,'drift',lambda *a: (_ for _ in ()).throw(spec.SpecError('timeout')))
    monkeypatch.setattr(service.runner,'run',lambda req,*a,**kw: ProviderResult(passing_review(req,'observed'),cost_usd=.01))
    artifacts={}
    with pytest.raises(ExecutionError):service._independent_verify(run['id'],run,p,service.runtime_settings.get(),artifacts)
    assert artifacts['verification']['verdict']=='unverified'
    assert artifacts['acceptance_ledger']['counts']=={'pass':1,'fail':0,'unverified':1}


def test_inspection_preserves_drift_evidence(app_env,monkeypatch):
    from factory.control.inspections import inspect_run
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'owner')
    config=service.inspections.configure(p['id'],enabled=True,interval_s=300,revision=0,actor_id=1)
    # Prevent scheduler races; call the same inspection job directly.
    service.stopping.set()
    rid=service.inspections.tick(config['next_at'])[0]
    service.cancels[rid]=threading.Event()
    monkeypatch.setattr(service.runner,'run',lambda req,*a,**kw: ProviderResult(passing_review(req,'observed'),cost_usd=.01))
    inspect_run(service,rid)
    run=store.get(rid)
    assert run['status']=='inspection_completed'
    assert run['artifacts']['spec_drift'][0]['status']=='pass'
    assert run['artifacts']['acceptance_ledger']['items'][-1]['spec_path']=='.spec/sample/spec.md'


@pytest.mark.parametrize('bootstrap',[False,True])
def test_normal_run_mounts_spec_and_archives_spec_with_code(app_env,monkeypatch,bootstrap):
    from tests.test_control_app import wait_state
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'owner')
    node=root/'.spec/sample/spec.md';node.write_text(document('greeting.txt'));commit(root,'govern greeting')
    from factory.control.autonomy import DEFAULT_POLICY
    service.policies.update(p['id'],{**DEFAULT_POLICY,'mode':'supervised'},0,'owner')
    original=service.runner.run;prompts=[]
    def runner(req,emit,cancel=None):
        prompts.append(req)
        if req.verification:return ProviderResult(passing_review(req,'observed greeting'),cost_usd=.01)
        result=original(req,emit,cancel)
        if not req.read_only:
            specfile=Path(req.workspace)/'.spec/sample/spec.md'
            specfile.write_text(specfile.read_text()+'\nGreeting now says hello world.\n')
        return result
    monkeypatch.setattr(service.runner,'run',runner)
    response=(client.post(f"/api/v2/projects/{p['id']}/spec-tree/generate",headers=headers,json={'idempotency_key':'pipeline-spec-01'}) if bootstrap else
              client.post('/api/v2/runs',headers=headers,json={'project_id':p['id'],'request':'Update greeting to hello world'}))
    assert response.status_code==201,response.text
    rid=response.json()['id'];run=wait_state(store,rid,{'awaiting_approval','needs_human'})
    assert run['status']=='awaiting_approval',run
    assert '.spec/sample/spec.md' in run['plan']['tasks'][0]['paths']
    service.approve(rid,run['revision'],'owner')
    done=wait_state(store,rid,{'ready_for_review','needs_human'})
    assert done['status']=='ready_for_review',done.get('artifacts')
    assert all('SPEC TREE' in req.prompt for req in prompts)
    assert any(req.verification for req in prompts) is bootstrap
    executor=next(req for req in prompts if not req.read_only)
    assert '人签要求' in executor.prompt
    assert done['artifacts']['spec_drift'][0]['status']=='pass'


def test_node_links_read_delivery_commit_before_merge(app_env):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'owner')
    command(root,'switch','-qc','spec-delivery')
    path='.spec/sample/child/spec.md';node=root/path;node.parent.mkdir();node.write_text(document('greeting.txt'))
    sha=commit(root,'new node');command(root,'switch','-q','main')
    run=store.create_run(p['id'],'generate')[0];store.update(run['id'],{'artifacts':{'commit':sha,'verification_commit':sha}})
    url=f"/api/v2/projects/{p['id']}/spec-tree"
    assert client.get(url+'/node',params={'path':path}).status_code==404
    data=client.get(url+'/node',params={'path':path,'run_id':run['id']})
    assert data.status_code==200 and data.json()['history'][0]['sha']==sha
    assert len(client.get(url,params={'run_id':run['id']}).json()['nodes'])==2
    unrelated=store.add_project({'name':'other','repository':'owner/other','workspace':str(root),'base_branch':'main'})
    foreign=store.create_run(unrelated['id'],'foreign')[0]
    assert client.get(url,params={'run_id':foreign['id']}).status_code==404
