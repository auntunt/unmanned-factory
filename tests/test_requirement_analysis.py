import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from factory.control import requirement_analysis as ra, fidelity
from factory.control.acceptance_ledger import criteria_for, coverage
from factory.control.modules import ModuleStore
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store, Conflict


def proposal(skill=None, reference=False):
    return {'spec_draft': {'goal': '构建团购工具', 'screens': [{'name': '首页', 'purpose': '浏览团购'}],
        'flows': ['浏览并选择商品'], 'data_model': ['商品名称与价格'], 'non_goals': ['不接入真实支付'], 'risks_assumptions': ['示例数据']},
        'recommended_skills': [{'id': skill['id'], 'version': skill['version'], 'reason': '界面需要一致的布局'}] if skill else [],
        'fidelity_target': {'reference': '美团', 'basis': '模型知识，未抓取外站', 'screens': [{'screen': '首页',
            'layout': ['顶部搜索与分类'], 'colors': ['黄色主色'], 'components': ['商品卡片'], 'interactions': ['分类筛选']}]} if reference else None}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    git('init', '-b', 'main'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.test')
    (root / 'README.md').write_text('Project')
    git('add', '.'); git('commit', '-m', 'seed')
    store = Store(tmp_path / 'control.db')
    project = store.add_project(dict(name='测试', repository='local/test', workspace=str(root), base_branch='main', checks={'check':['true']}, budget_usd=10, requirement_analysis_budget_usd=5))
    requests = []
    result = proposal()
    class Runner:
        def run(self, request, emit, cancel=None):
            requests.append(request)
            return ProviderResult(text=json.dumps(result, ensure_ascii=False), cost_usd=0.2)
    svc = Service(store, runner=Runner(), profiles={r:{'provider':'claude','model':'test'} for r in ('planner','cheap','standard','strong')})
    queued = []
    svc._submit = lambda fn, rid: queued.append((fn.__name__, rid))
    run, _ = store.create_run(project['id'], '像美团的团购工具', source={'type':'web','operation':'general'})
    svc.cancels[run['id']] = threading.Event()
    yield SimpleNamespace(store=store, svc=svc, run=run, project=project, requests=requests, result=result, queued=queued, git=git)
    svc.close()


def test_confirm_stores_delivery_type_inferred_from_request_text(env):
    """The real confirm() path must write delivery_type_inferred from the request text."""
    env.result.update(proposal())
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    assert run['status'] == 'awaiting_spec_confirmation'
    body = ra.Confirmation(revision=run['revision'], spec_draft=run['spec_draft'], selected_skills=[])
    ra.confirm(env.svc, run['id'], body, 'owner')
    confirmed = env.store.get(run['id'])
    # The fixture request is '像美团的团购工具' which is ambiguous; delivery_type_inferred should be None.
    assert confirmed.get('delivery_type_inferred') is None


def test_confirm_stores_cli_delivery_type_from_request(env):
    """When request text clearly says CLI, confirm() infers delivery_type_inferred='cli'."""
    env.store.update(env.run['id'], {'status': 'cancelled'})
    run2, _ = env.store.create_run(env.project['id'], '做一个命令行工具',
        source={'type': 'web', 'operation': 'general', 'original_request': '做一个命令行工具'})
    import threading
    env.svc.cancels[run2['id']] = threading.Event()
    env.svc._analyze(run2['id'])
    run = env.store.get(run2['id'])
    assert run['status'] == 'awaiting_spec_confirmation'
    body = ra.Confirmation(revision=run['revision'], spec_draft=run['spec_draft'], selected_skills=[])
    ra.confirm(env.svc, run2['id'], body, 'owner')
    confirmed = env.store.get(run2['id'])
    assert confirmed['delivery_type_inferred'] == 'cli'


def test_confirm_stores_service_from_spec_draft_goal(env):
    """When spec_draft goal says '网站', the inference picks service even if request text is ambiguous."""
    env.result.update(proposal())
    env.result['spec_draft']['goal'] = '构建一个团购网站'
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    body = ra.Confirmation(revision=run['revision'],
        spec_draft=ra.SpecDraft(**{**run['spec_draft'], 'goal': '构建一个团购网站'}),
        selected_skills=[])
    ra.confirm(env.svc, run['id'], body, 'owner')
    confirmed = env.store.get(run['id'])
    assert confirmed['delivery_type_inferred'] == 'service'


def test_general_waits_for_single_confirmation_and_freezes_skills(env):
    skill = ModuleStore(env.store).list()[0]
    env.result.update(proposal(skill, True))
    old = env.git('rev-parse','HEAD')
    env.svc.start_plan(env.run['id'])
    assert env.queued == [('_analyze', env.run['id'])]
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    assert run['status'] == 'awaiting_spec_confirmation'
    assert run['plan'] is None
    assert env.git('rev-parse','HEAD') == old
    from pathlib import Path
    raw = (Path(run['requirement_workspace']) / run['requirement_spec_path']).read_text()
    assert '## raw source' in raw and '构建团购工具' in raw and '尚未人签' in raw
    assert env.requests[0].tools_disabled and env.requests[0].max_budget_usd == 5
    body = ra.Confirmation(revision=run['revision'], spec_draft=run['spec_draft'], selected_skills=run['recommended_skills'], fidelity_target=run['fidelity_target'])
    ra.confirm(env.svc, run['id'], body, 'owner')
    signed = env.store.get(run['id'])
    assert signed['spec_confirmation']['actor'] == 'owner'
    assert signed['module_snapshot'][0]['id'] == skill['id']
    assert signed['mount_snapshot']['digest']
    assert env.queued[-1][0] == '_plan'
    assert not ra.required(signed)
    with pytest.raises(Conflict):
        ra.confirm(env.svc, run['id'], body, 'owner')
    assert criteria_for(signed)[-1]['id'].startswith('fidelity:')


def test_auto_confirm_is_audited_and_non_general_is_optional(env):
    env.store.update(env.run['id'], {'status':'cancelled'})
    env.store.update_project(env.project['id'], {'auto_spec_confirm':True}, 1, 'owner')
    env.store.update(env.run['id'], {'status':'received'})
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    assert run['spec_confirmation']['automatic']
    assert run['status'] == 'received'
    assert any(e['type']=='spec.auto_confirmed' for e in env.store.events(run['id']))
    assert not ra.required({'source':{'operation':'bugfix'}})
    assert ra.required({'source':{'operation':'bugfix','requirement_analysis':True}})


def test_automatic_submission_freezes_spec_without_changing_project_policy(env):
    env.store.update(env.run['id'], {'source': {**env.run['source'], 'interaction_mode': 'automatic'}})
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    assert run['status'] == 'received'
    assert run['spec_confirmation']['automatic'] is True
    assert run['spec_confirmation']['policy'] == 'submission'
    assert run['spec_confirmation']['actor'] == 'submission-policy'
    assert not env.store.project(env.project['id']).get('auto_spec_confirm', False)
    assert not run['source'].get('execute_deploy')
    assert run['mount_snapshot']['digest']
    assert env.queued[-1][0] == '_plan'
    assert any(e['type'] == 'spec.auto_confirmed' for e in env.store.events(run['id']))


def test_automatic_confirmation_cannot_be_called_without_delegation(env):
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    with pytest.raises(Conflict, match='未授权自动确认'):
        ra.confirm(env.svc, run['id'], ra.Confirmation(revision=run['revision'],
            spec_draft=run['spec_draft'], selected_skills=[]), 'submission-policy', automatic=True)
    assert env.store.get(run['id'])['status'] == 'awaiting_spec_confirmation'


def test_analysis_cost_never_uses_coding_budget(env):
    env.svc._emit(env.run['id'], 'usage.recorded', {'profile':'requirement_analysis','cost_usd':1.5,'call_id':'analysis'})
    assert env.svc._remaining_dollar_budget(env.run['id'], env.project).remaining_usd == 10
    assert env.svc._usage(env.run['id'])['known_cost_usd'] == 1.5


def test_fidelity_unlike_fails_and_like_requires_real_screenshot(env):
    run = {**env.run, **proposal(reference=True), 'spec_confirmation':{'actor':'owner'}}
    items = criteria_for(run)
    verdict = {'verdict':'pass','reason':'能跑', 'criteria':[{'id':c['id'],'status':'pass','evidence':'真实检查','judgment':'unlike' if c.get('class')=='fidelity' else 'like'} for c in items]}
    ledger = coverage(items, verdict, 'HEAD')
    assert fidelity.enforce(env.store, run['id'], run, verdict, ledger)['verdict'] == 'fail'
    assert ledger['counts']['fail'] == 4
    from factory.control.recovery import _continuous_resume_stage
    mismatch = fidelity.enforce(env.store, run['id'], run, verdict, ledger)
    assert not mismatch.get('error_type')
    assert _continuous_resume_stage({'commit':'checked', 'tasks':[{'status':'verified'}], 'verification':mismatch}) is None
    for row in verdict['criteria']: row['judgment']='like'
    ledger = coverage(items, verdict, 'HEAD')
    assert fidelity.enforce(env.store, run['id'], run, verdict, ledger)['verdict'] == 'unverified'
    assert fidelity.criteria({**run,'fidelity_target':None}) == []


def test_confirmed_general_goes_to_execution_without_second_approval(env):
    env.svc.start_plan(env.run['id'])
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    ra.confirm(env.svc, run['id'], ra.Confirmation(revision=run['revision'], spec_draft=run['spec_draft'], selected_skills=[]), 'owner')
    env.svc._plan(run['id'])
    planned = env.store.get(run['id'])
    assert planned['status'] == 'queued', env.store.events(run['id'])
    assert planned['execution_mode'] == 'continuous'
    assert env.queued[-1][0] == '_run'
    assert 'CONFIRMED REQUIREMENT CONTRACT' in planned['plan']['tasks'][0]['prompt']


def test_http_default_general_requires_confirmation_and_rejects_stale_revision(env):
    from fastapi.testclient import TestClient
    from factory.control.app import create_app
    from tests.test_control_app import login
    root = __import__('pathlib').Path(env.project['workspace'])
    app = create_app(data_dir=root.parent, workspace_root=root.parent, public_origin='http://testserver', service=env.svc)
    app.state.auth.create_user('owner', 'a-long-test-password')
    with TestClient(app) as client:
        headers = login(client)
        response = client.post('/api/v2/runs', json={'project_id':env.project['id'],'request':'一个小工具','idempotency_key':'same-request-1'}, headers=headers)
        assert response.status_code == 201
        rid = response.json()['id']
        assert env.queued[-1] == ('_analyze',rid)
        env.svc.cancels[rid] = threading.Event()
        env.svc._analyze(rid)
        run = client.get('/api/v2/runs/'+rid).json()
        assert run['status'] == 'awaiting_spec_confirmation'
        body = {'revision':run['revision'],'spec_draft':run['spec_draft'],'selected_skills':[],'action':'waive'}
        assert client.post(f'/api/v2/runs/{rid}/confirm-spec',json={**body,'revision':999},headers=headers).status_code==409
        assert client.post(f'/api/v2/runs/{rid}/confirm-spec',json=body,headers=headers).status_code==200
        assert client.post(f'/api/v2/runs/{rid}/confirm-spec',json=body,headers=headers).status_code==409
        duplicate = client.post('/api/v2/runs',json={'project_id':env.project['id'],'request':'一个小工具','idempotency_key':'same-request-1'},headers=headers)
        assert duplicate.json()['id']==rid


def test_skill_version_is_frozen_and_unknown_selection_is_rejected(env):
    skill=ModuleStore(env.store).list()[0]
    env.result.update(proposal(skill))
    env.svc._analyze(env.run['id'])
    run=env.store.get(env.run['id'])
    with pytest.raises(ValueError):
        ra.confirm(env.svc,run['id'],ra.Confirmation(revision=run['revision'],spec_draft=run['spec_draft'],selected_skills=[{'id':'unknown','version':1,'reason':'injected'}]),'owner')
    assert env.store.get(run['id'])['status']=='awaiting_spec_confirmation'


def test_http_automatic_submission_is_audited_and_idempotent(env):
    from fastapi.testclient import TestClient
    from pathlib import Path
    from factory.control.app import create_app
    from tests.test_control_app import login
    root = Path(env.project['workspace'])
    app = create_app(data_dir=root.parent, workspace_root=root.parent,
                     public_origin='http://testserver', service=env.svc)
    app.state.auth.create_user('owner', 'a-long-test-password')
    with TestClient(app) as client:
        headers = login(client)
        body = {'project_id': env.project['id'], 'request': '制作一个订单应用',
                'interaction_mode': 'automatic', 'idempotency_key': 'automatic-request-1'}
        response = client.post('/api/v2/runs', json=body, headers=headers)
        assert response.status_code == 201
        rid = response.json()['id']
        env.svc.cancels[rid] = threading.Event()
        env.svc._analyze(rid)
        run = client.get('/api/v2/runs/' + rid).json()
        assert run['spec_confirmation']['policy'] == 'submission'
        assert run['status'] == 'received'
        assert client.post('/api/v2/runs', json=body, headers=headers).json()['id'] == rid
        assert client.post('/api/v2/runs', json={**body, 'interaction_mode': 'review'}, headers=headers).status_code == 409
        assert client.post('/api/v2/runs', json={**body, 'interaction_mode': 'unknown'}, headers=headers).status_code == 422


def test_fidelity_pass_requires_current_verifier_screenshot_and_raw_source_is_immutable(env):
    run={**env.run,**proposal(reference=True),'spec_confirmation':{'actor':'owner'}}
    env.store.append(run['id'],'browser.observed',{'ok':True,'screenshot_path':'verified/home.png'},task_id='verification')
    eid=env.store.events(run['id'])[-1]['id']
    items=criteria_for(run)
    verdict={'verdict':'pass','reason':'逐项相似','criteria':[{'id':c['id'],'status':'pass','evidence':'首页黄色搜索栏和分类卡片，筛选已验证','judgment':'like','screenshot_event_ids':[eid]} for c in items]}
    assert fidelity.enforce(env.store,run['id'],run,verdict,coverage(items,verdict,'HEAD'))['verdict']=='pass'
    env.store.append(run['id'],'verification.workspace_created',{'commit':'new'})
    assert fidelity.enforce(env.store,run['id'],run,verdict,coverage(items,verdict,'HEAD'))['verdict']=='unverified'


def test_budget_resume_preserves_ledger_and_reuses_existing_continuation(env):
    from factory.control.budget_resume import resume
    rid=env.run['id']
    env.store.update(rid,{'status':'needs_human','revision':1,'spec_confirmation':{'actor':'owner'},'artifacts':{'budget_exhausted':True}})
    env.svc._emit(rid,'usage.recorded',{'profile':'standard','call_id':'old','cost_usd':10})
    calls=[]
    env.svc.continue_run=lambda *args: calls.append(args) or {'execution_resume':{'resume_stage':'verification'}}
    result=resume(env.svc,rid,1,0,'owner')
    assert result['execution_resume']['resume_stage']=='verification'
    assert calls==[(rid,'',1,0,'owner')]
    assert env.svc._usage(rid)['known_cost_usd']==10
    assert env.svc._remaining_dollar_budget(rid,env.project).remaining_usd==10


def test_recovery_reuses_parsed_draft_without_another_model_call(env, monkeypatch):
    original = ra.save_spec
    monkeypatch.setattr(ra, 'save_spec', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk interrupted')))
    env.svc._analyze(env.run['id'])
    assert env.store.get(env.run['id'])['status'] == 'needs_human'
    assert len(env.requests) == 1
    monkeypatch.setattr(ra, 'save_spec', original)
    env.store.update(env.run['id'], {'status': 'received'})
    env.svc._analyze(env.run['id'])
    assert env.store.get(env.run['id'])['status'] == 'awaiting_spec_confirmation'
    assert len(env.requests) == 1


def test_confirmed_raw_source_cannot_be_rewritten(env):
    from pathlib import Path
    env.svc._analyze(env.run['id'])
    run=env.store.get(env.run['id'])
    ra.confirm(env.svc,run['id'],ra.Confirmation(revision=run['revision'],spec_draft=run['spec_draft'],selected_skills=[]),'owner')
    run=env.store.get(run['id'])
    assert ra.raw_source_evidence(run, run['requirement_workspace'], 'HEAD')[0]['status']=='pass'
    path=Path(run['requirement_workspace'])/run['requirement_spec_path']
    path.write_text(path.read_text().replace('构建团购工具','悄悄改变目标'))
    ra._git(path.parents[3], 'add', run['requirement_spec_path'])
    ra._git(path.parents[3], '-c','user.name=Test','-c','user.email=test@example.test','commit','-m','change intent')
    assert ra.raw_source_evidence(run, run['requirement_workspace'], 'HEAD')[0]['status']=='fail'


def test_platform_operation_text_does_not_authorize_or_inflate_continuous_risk():
    from factory.control.planning import triage
    compiled='Fix typo\n平台说明：权限和授权沿用现有规则'
    run={'request':compiled,'source':{'original_request':'Fix typo','compiled_request_sha256':__import__('hashlib').sha256(compiled.encode()).hexdigest()},'execution_mode':'continuous'}
    plan={'title':'Update greeting','summary':'Fix typo','tasks':[{'prompt':compiled,'paths':['README.md']}], 'questions':[], 'acceptance':[],'risk':'low','complexity':'low'}
    projected=Service._triage_plan(run,plan)
    assert projected['tasks'][0]['prompt']=='Fix typo'
    assert plan['tasks'][0]['prompt']==compiled
    assert Service._authorization_request(run)=='Fix typo'
    dangerous={**run,'source':{**run['source'],'original_request':'change authentication permissions'}}
    assert triage(Service._triage_plan(dangerous,plan),Service._authorization_request(dangerous),auto_enabled=True)['risk']=='high'
    assert Service._triage_plan({**run,'execution_mode':'dag'},plan)==plan
    assert Service._submitted_request({**run,'request':'new request: change permissions'})=='new request: change permissions'


@pytest.mark.parametrize('budget_pause', [False, True])
@pytest.mark.parametrize('automatic', [False, True])
def test_general_complete_delivery_uses_scope_and_independent_acceptance(env, monkeypatch, budget_pause, automatic):
    from pathlib import Path
    from tests.review_helpers import passing_review
    requests=[]
    class FullRunner:
        def available(self): return [{'id':'claude','installed':True}]
        def run(self,request,emit,cancel=None):
            requests.append(request)
            if request.prompt.startswith(ra.IDENTITY):
                return ProviderResult(json.dumps(proposal()),cost_usd=0.2)
            if request.read_only:
                return ProviderResult(passing_review(request,'功能检查通过'),cost_usd=0.1)
            emit('assistant.message',{'text':json.dumps({'scope_declaration':{'files':[{'path':'README.md','spec_nodes':[]}]}})})
            Path(request.workspace,'README.md').write_text('Delivered project\n')
            return ProviderResult('Implemented and checked',cost_usd=0.3)
    env.svc.runner=FullRunner()
    rid=env.run['id']
    if automatic:
        env.store.update(rid, {'source': {**env.run['source'], 'interaction_mode': 'automatic'}})
    env.svc.start_plan(rid); env.svc._analyze(rid)
    waiting=env.store.get(rid)
    if automatic:
        assert waiting['spec_confirmation']['policy'] == 'submission'
        assert waiting['status'] == 'received'
    else:
        ra.confirm(env.svc,rid,ra.Confirmation(revision=waiting['revision'],spec_draft=waiting['spec_draft'],selected_skills=[]),'owner')
    original_verify=env.svc._independent_verify
    if budget_pause:
        from factory.control.execution import ExecutionError
        def paused_verify(rid,run,project,configuration,artifacts):
            env.svc._budget_stop_artifacts(rid,project,'验收预算暂停',artifacts)
            raise ExecutionError('验收预算暂停',artifacts=artifacts)
        monkeypatch.setattr(env.svc,'_independent_verify',paused_verify)
    env.svc._plan(rid); env.svc._run(rid)
    run=env.store.get(rid)
    if budget_pause:
        from factory.control.budget_resume import resume
        assert run['status']=='needs_human'
        assert run['artifacts']['budget_exhausted']
        monkeypatch.setattr(env.svc,'_independent_verify',original_verify)
        resumed=resume(env.svc,rid,run['revision'],run.get('resume_count',0),'owner')
        assert resumed['execution_resume']['resume_stage']=='verification'
        env.svc._run(rid)
        run=env.store.get(rid)
    assert run['status']=='ready_for_review', (run.get('error'),run.get('artifacts',{}).get('acceptance_ledger'))
    ledger=run['artifacts']['acceptance_ledger']
    assert any(i['id']=='requirement:raw_source' and i['status']=='pass' for i in ledger['items'])
    assert any(i['id']=='scope:reconciliation' and i['status']=='pass' for i in ledger['items'])
    assert len([r for r in requests if not r.read_only])==1
    expected_confirmation = 'spec.auto_confirmed' if automatic else 'spec.confirmed'
    assert len([e for e in env.store.events(rid) if e['type']==expected_confirmation])==1
    assert env.svc._usage(rid,profile='requirement_analysis')['known_cost_usd']==0.2
    # Initial GitHub upload can still safely advance the untouched project baseline.
    from factory.control.github_publication import GitHubPublication
    published={**run,'artifacts':{**run['artifacts'],'publication_type':'initial'}}
    synced=GitHubPublication(env.svc).sync_initial_baseline(published)
    assert synced['status']=='synced', synced
    assert env.git('rev-parse','HEAD')==run['artifacts']['commit']


@pytest.mark.parametrize('action', ['confirm-spec', 'resume-budget'])
def test_new_actions_preserve_ownership_project_grants_and_csrf(env, action):
    from fastapi.testclient import TestClient
    from factory.control.app import create_app
    from pathlib import Path
    app=create_app(data_dir=Path(env.project['workspace']).parent,workspace_root=Path(env.project['workspace']).parent,public_origin='http://testserver',service=env.svc)
    member=app.state.auth.create_user('member','a-long-test-password',role='member')
    env.svc.governance.assign(member['id'],[env.project['id']],'owner')
    with TestClient(app) as client:
        login=client.post('/api/auth/login',json={'username':'member','password':'a-long-test-password'},headers={'Origin':'http://testserver'})
        headers={'Origin':'http://testserver','X-CSRF-Token':login.json()['csrf_token']}
        rid=env.run['id']; url=f'/api/v2/runs/{rid}/{action}'
        assert client.post(url,json={},headers=headers).status_code==403
        env.store.update(rid,{'source':{**env.run['source'],'actor_id':member['id']}})
        assert client.post(url,json={},headers={'Origin':'http://testserver'}).status_code==403
        # Authorized requests reach body validation; no new blanket write access.
        # resume-budget is the exception: raising a ceiling is an admin decision,
        # so project ownership never buys a member past the role check.
        assert client.post(url,json={},headers=headers).status_code==(403 if action=='resume-budget' else 422)
        env.svc.governance.assign(member['id'],[],'owner')
        assert client.post(url,json={},headers=headers).status_code==403


def test_analysis_opt_in_participates_in_fingerprint(env):
    from fastapi.testclient import TestClient
    from factory.control.app import create_app
    from tests.test_control_app import login
    from pathlib import Path
    root=Path(env.project['workspace']).parent
    app=create_app(data_dir=root,workspace_root=root,public_origin='http://testserver',service=env.svc)
    app.state.auth.create_user('owner','a-long-test-password')
    with TestClient(app) as client:
        headers=login(client)
        body={'project_id':env.project['id'],'request':'排查启动','operation':'startup','idempotency_key':'analysis-opt-in'}
        assert client.post('/api/v2/runs',json=body,headers=headers).status_code==201
        assert client.post('/api/v2/runs',json={**body,'requirement_analysis':True},headers=headers).status_code==409


@pytest.mark.parametrize('endpoint', ['resume-budget', 'continue'])
def test_analysis_can_resume_before_first_revision_via_http(env, endpoint):
    from fastapi.testclient import TestClient
    from factory.control.app import create_app
    from tests.test_control_app import login
    from pathlib import Path
    root=Path(env.project['workspace']).parent
    app=create_app(data_dir=root,workspace_root=root,public_origin='http://testserver',service=env.svc)
    app.state.auth.create_user('owner','a-long-test-password')
    rid=env.run['id']
    env.store.update(rid,{'status':'needs_human'})
    assert env.store.get(rid)['revision']==0
    with TestClient(app) as client:
        headers=login(client)
        response=client.post(f'/api/v2/runs/{rid}/{endpoint}',json={'revision':0,'resume_count':0},headers=headers)
        assert response.status_code==200, response.text
        assert response.json()['status']=='received'
        assert response.json()['requirement_analysis_credit_usd']==5
        assert env.queued[-1]==('_analyze',rid)
        assert client.post(f'/api/v2/runs/{rid}/{endpoint}',json={'revision':0,'resume_count':0},headers=headers).status_code==409

@pytest.mark.parametrize('valid,error_kind', [(True,'budget_exhausted'), (False,'budget_exhausted'), (True,'timeout')])
def test_budget_boundary_salvages_only_complete_valid_analysis(env, valid, error_kind):
    from factory.control.providers import ProviderError
    value = proposal(reference=True)
    value['spec_draft']['screens'] = [{'name':f'页面{i}', 'purpose':'商品浏览'} for i in range(20)]
    value['fidelity_target']['screens'] = [{**value['fidelity_target']['screens'][0], 'screen':f'页面{i}',
        'layout':['搜索与分类，保留导航与商品卡片。' * 100]} for i in range(20)]
    text = json.dumps(value, ensure_ascii=False) if valid else '{"spec_draft":'
    class Runner:
        def run(self, request, emit, cancel):
            raise ProviderError('budget boundary', error_kind=error_kind,
                partial_result=ProviderResult(text, cost_usd=5.1, tokens_in=100, tokens_out=9000))
    env.svc.runner = Runner()
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    assert run['status'] == ('awaiting_spec_confirmation' if valid and error_kind=='budget_exhausted' else 'needs_human')
    assert run['plan'] is None
    assert env.svc._usage(run['id'],profile='requirement_analysis')['known_cost_usd'] == 5.1
    assert env.svc._remaining_dollar_budget(run['id'],env.project).remaining_usd == 10
    if valid and error_kind=='budget_exhausted':
        assert len(run['fidelity_target']['screens']) == 20
        assert any(e['type']=='requirement_analysis.budget_salvaged' for e in env.store.events(run['id']))

@pytest.mark.parametrize('entry', ['resume', 'continue'])
def test_analysis_renewal_reruns_analysis_and_still_requires_confirmation(env, entry):
    from factory.control.budget_resume import resume
    rid = env.run['id']
    env.store.update(rid, {'status':'needs_human'})
    env.svc._emit(rid,'usage.recorded',{'profile':'requirement_analysis','call_id':'old','cost_usd':5})
    if entry == 'resume':
        resume(env.svc,rid,0,0,'owner')
    else:
        env.svc.continue_run(rid,'',0,0,'owner')
    assert env.queued == [('_analyze',rid)]
    env.svc._analyze(rid)
    assert env.requests[-1].max_budget_usd == 5
    assert env.requests[-1].model == 'sonnet'
    assert env.store.get(rid)['status'] == 'awaiting_spec_confirmation'
    assert env.store.get(rid)['plan'] is None
    assert env.store.get(rid)['requirement_analysis_credit_usd'] == 5


def test_analysis_independent_profile_override(env, monkeypatch):
    monkeypatch.setenv('FACTORY_REQUIREMENT_ANALYSIS_MODEL','my-sonnet')
    env.svc._analyze(env.run['id'])
    assert env.requests[-1].model == 'my-sonnet'


def test_missing_analysis_budget_means_monitoring_even_with_unknown_old_usage(env):
    # Legacy project rows never acquired a hidden $5 ceiling merely by upgrading.
    with env.store.connect() as db:
        row = env.store.project(env.project['id'])
        row.pop('requirement_analysis_budget_usd')
        db.execute('UPDATE projects SET data=? WHERE id=?', (json.dumps(row), row['id']))
    env.store.append(env.run['id'], 'usage.recorded', {'profile':'requirement_analysis', 'cost_usd':None, 'max_budget_usd':2})
    env.svc._analyze(env.run['id'])
    assert env.requests[-1].max_budget_usd is None
    assert env.store.get(env.run['id'])['status'] == 'awaiting_spec_confirmation'


def test_retry_preserves_analysis_contract_and_old_run_links_forward(env):
    from factory.control.autonomy_routes import _attention
    env.store.update(env.run['id'], {'status':'needs_human'})
    original = env.store.get(env.run['id'])
    retried, created = env.svc.agents.create_retry(original, 'owner', 1, [])
    assert created
    assert retried['source']['operation'] == 'general'
    assert ra.required(retried)
    env.svc.start_plan(retried['id'])
    assert env.queued[-1] == ('_analyze', retried['id'])
    prior = env.store.get(original['id'])
    assert prior['status'] == 'needs_human'  # History is retained, not rewritten.
    assert prior['retry_run_id'] == retried['id']
    assert _attention(prior, {}) is None
    assert env.svc.agents.create_retry(original, 'owner', 1, [])[0]['id'] == retried['id']


@pytest.mark.parametrize('heading', ['expanded', 'expanded spec'])
def test_raw_source_gate_allows_implementation_updates_but_protects_intent(env, heading):
    from pathlib import Path
    env.svc._analyze(env.run['id'])
    run = env.store.get(env.run['id'])
    ra.confirm(env.svc, run['id'], ra.Confirmation(revision=run['revision'],
        spec_draft=run['spec_draft'], selected_skills=[]), 'owner')
    run = env.store.get(run['id'])
    workspace = run['requirement_workspace']
    path = Path(workspace) / run['requirement_spec_path']
    text = path.read_text().replace('## expanded spec', '## expanded')
    text = text.replace('## expanded', '## ' + heading)
    path.write_text(text)
    def checkpoint(message):
        ra._git(workspace, 'add', run['requirement_spec_path'])
        ra._git(workspace, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                'commit', '--allow-empty', '-m', message)
        return ra._git(workspace, 'rev-parse', 'HEAD').strip()
    # Simulate both pre-fix production specs and newly generated canonical specs.
    run['requirement_spec_commit'] = checkpoint('confirmed baseline')
    path.write_text(text.replace('code:\nrelated:', 'code:\n  - README.md\nrelated:\n  - README.md')
                    .replace('由执行阶段维护实现细节，禁止改写 raw source。', '已实现商品浏览；验证分类筛选。'))
    checkpoint('implementation metadata and expanded documentation')
    assert ra.raw_source_evidence(run, workspace, 'HEAD')[0]['status'] == 'pass'
    path.write_text(path.read_text().replace('构建团购工具', '篡改原始目标'))
    checkpoint('change original intent')
    assert ra.raw_source_evidence(run, workspace, 'HEAD')[0]['status'] == 'fail'
