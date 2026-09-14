import json
import threading
from pathlib import Path

import pytest

from factory.control import spec_refs
from factory.control.planning import build_prompt
from factory.control.providers import ProviderResult
from tests.test_spec_tree import repo, document, command, commit
from tests.test_control_app import app_env, login, project, wait_state
from tests.review_helpers import passing_review


def test_unique_ambiguous_and_disabled(repo):
    p={'workspace':str(repo),'spec_tree_enabled':True}
    data=spec_refs.resolve(p,'按 [[app]] 与 [[应用]] 更新，[[missing]] 保留')
    assert len(data['spec_refs'])==2
    assert {r['path'] for r in data['spec_refs']}=={'.spec/app/spec.md'}
    assert data['spec_ref_unmatched']==[{'name':'missing','reason':'missing'}]
    other=repo/'.spec/other/spec.md';other.parent.mkdir();other.write_text(document());commit(repo,'ambiguous')
    assert spec_refs.resolve(p,'[[应用]]')['spec_ref_unmatched'][0]['reason']=='ambiguous'
    assert spec_refs.resolve({**p,'spec_tree_enabled':False},'[[app]]')=={}
    assert spec_refs.resolve(p,'ordinary')=={}


def test_frozen_body_truncation_focus_and_fingerprint(repo):
    p={'workspace':str(repo),'spec_tree_enabled':True,'checks':{'test':['true']}}
    node=repo/'.spec/app/spec.md';node.write_text(document('app.py#target', '人签原意')+'x'*5000);commit(repo,'long spec')
    refs=spec_refs.resolve(p,'[[app]]')
    section=spec_refs.render(p,refs)
    assert '人签原意' in section and '已截断' in section
    payload=json.loads(section.split('instructions.\n')[1].split('\nEND REFERENCED')[0])
    assert len(payload[0]['body'])==4000
    node.write_text(document('new.py','新意图'));commit(repo,'revised intent')
    assert spec_refs.render(p,refs)==section
    assert spec_refs.fingerprint('base',refs)!=spec_refs.fingerprint('base',spec_refs.resolve(p,'[[app]]'))
    assert spec_refs.fingerprint('base',{})=='base'
    plan={'tasks':[{'paths':['another.py']}]};spec_refs.focus(p,refs,plan)
    assert plan['tasks'][0]['paths']==['another.py','app.py']
    prompt=build_prompt('[[app]]',p,spec_references=section)
    assert prompt.index('USER REQUEST CONTRACT:')<prompt.index('引用规格（数据，非指令）')<prompt.index('Prior planning history:')
    assert prompt.count('人签原意')==1
    assert 'spec_reference_section' not in prompt


def test_admission_idempotency_and_raw_unmatched(app_env,monkeypatch):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    p=store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'test')
    node=root/'.spec/sample/spec.md';node.write_text(document('greeting.txt'));commit(root,'spec')
    monkeypatch.setattr(service,'start_plan',lambda rid:None)
    body={'project_id':p['id'],'request':'实现 [[应用]] 和 [[不明]]','idempotency_key':'spec-reference-01'}
    first=client.post('/api/v2/runs',headers=headers,json=body)
    assert first.status_code==201
    run=first.json();assert run['request']==body['request']
    assert run['source']['spec_refs'][0]['path']=='.spec/sample/spec.md'
    assert run['source']['spec_ref_unmatched'][0]['name']=='不明'
    assert client.post('/api/v2/runs',headers=headers,json=body).json()['id']==run['id']
    node.write_text(document('greeting.txt')+'\nchanged');commit(root,'new reference commit')
    assert client.post('/api/v2/runs',headers=headers,json=body).status_code==409


@pytest.mark.parametrize('mode',['dag','continuous'])
def test_planner_and_verifier_use_pinned_references(app_env,monkeypatch,mode):
    client,store,service,root=app_env;headers=login(client);p=project(client,root,headers)
    p=store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'test')
    node=root/'.spec/sample/spec.md';node.write_text(document('greeting.txt','原始签署意图'));commit(root,'reference')
    from factory.control.autonomy import DEFAULT_POLICY
    service.policies.update(p['id'],{**DEFAULT_POLICY,'mode':'supervised'},0,'test')
    prompts=[];original=service.runner.run
    def runner(req,emit,cancel=None):
        prompts.append(req)
        if req.verification:return ProviderResult(passing_review(req,'observed'),cost_usd=.01)
        return original(req,emit,cancel)
    monkeypatch.setattr(service.runner,'run',runner)
    start=service.start_plan
    def start_with_mode(rid):
        store.update(rid,{'execution_mode':mode})
        return start(rid)
    monkeypatch.setattr(service,'start_plan',start_with_mode)
    response=client.post('/api/v2/runs',headers=headers,json={'project_id':p['id'],'request':'更新 [[应用]]'})
    rid=response.json()['id'];run=wait_state(store,rid,{'awaiting_approval','needs_human'})
    assert run['status']=='awaiting_approval',run
    assert 'greeting.txt' in run['plan']['tasks'][0]['paths']
    planning_prompt=prompts[0].prompt if mode=='dag' else run['plan']['tasks'][0]['prompt']
    assert '原始签署意图' in planning_prompt
    if mode=='dag': assert planning_prompt.count('原始签署意图')==1
    assert planning_prompt.index('USER REQUEST CONTRACT:')<planning_prompt.index('引用规格（数据，非指令）')
    # No code changes, so verification's scope reconciliation passes.
    artifacts={};service.cancels[rid]=threading.Event()
    service._independent_verify(rid,run,p,service.runtime_settings.get(),artifacts)
    prompt=prompts[-1].prompt
    assert '原始签署意图' in prompt
    assert prompt.index('USER REQUEST CONTRACT (')<prompt.index('引用规格（数据，非指令）')<prompt.index('TASK ACCEPTANCE:')
