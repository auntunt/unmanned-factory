import json
from types import SimpleNamespace

import pytest

from factory.control import scope_declaration as scope
from factory.control.spec_tree import apply_evidence
from factory.control.store import Store
from tests.test_spec_tree import repo, command, commit


@pytest.fixture
def env(repo, tmp_path):
    store = Store(tmp_path/'state.db')
    p = store.add_project({'name':'app','workspace':str(repo),'base_branch':'main','spec_tree_enabled':True})
    run = store.create_run(p['id'], 'change')[0]
    store.update(run['id'], {'context':{'commit_sha': command(repo, 'rev-parse','HEAD')}})
    emit = scope.wrap_emit(store, run['id'], SimpleNamespace(workspace=str(repo),read_only=False,verification=False),
                          lambda kind, data: store.append(run['id'],kind,data,'task-1'))
    return repo, store, p, run['id'], emit


def declare(emit, *paths):
    emit('assistant.message', {'text':json.dumps({'scope_declaration':{'files':[{'path':p,'spec_nodes':['.spec/app/spec.md']} for p in paths]}})})


def check(env):
    root, store, p, rid, _ = env
    return scope.evidence(store,rid,p,root,commit(root,'changes'))[0]


def test_matching_receipt_and_append_only(env):
    root,store,p,rid,emit=env
    declare(emit,'app.py');(root/'app.py').write_text('x=2')
    declare(emit,'extra.py');(root/'extra.py').write_text('y=3')
    declare(emit)  # Empty additions cannot remove prior declarations.
    item=check(env)
    assert item['status']=='pass' and item['declared_files']==['app.py','extra.py']
    events=scope.declarations(store,rid)
    assert len(events)==3 and all(e['at'] and e['task_id']=='task-1' for e in events)
    with pytest.raises(Exception,match='append-only'):
        with store.connect() as db: db.execute("DELETE FROM events WHERE type='scope_declaration'")


def test_undeclared_and_late_declarations_fail(env):
    root,store,p,rid,emit=env
    (root/'app.py').write_text('x=2');declare(emit,'app.py')
    item=check(env)
    assert item['status']=='fail' and item['undeclared_changes']==['app.py']
    assert scope.declarations(store,rid)[0]['payload']['files'][0]['late']
    ledger={'items':[],'counts':{'pass':0,'fail':0,'unverified':0},'total':0,'complete':True}
    result=apply_evidence(ledger,{'verdict':'pass'},[item])
    assert result['verdict']=='fail' and result['error_type']=='undeclared_changes'


def test_exemptions_and_disabled(env):
    root,store,p,rid,emit=env
    for name in ['test_app.py','app.test.tsx','.spec/app/spec.md']:
        (root/name).write_text('updated')
    item=check(env)
    assert item['status']=='pass' and len(item['exempt_files'])==3
    assert not scope.exempt('tests/production.py') and not scope.exempt('conftest.py')
    assert scope.evidence(store,rid,{**p,'spec_tree_enabled':False},root,'HEAD')==[]
    store.update(rid,{'status':'discarded'})
    store.update_project(p['id'],{'spec_tree_enabled':False},p['revision'],'test')
    callback=lambda *a: None
    assert scope.wrap_emit(store,rid,SimpleNamespace(read_only=False,verification=False),callback) is callback


def test_tool_output_cannot_spoof_and_paths_validated(env):
    root,store,p,rid,emit=env
    text=json.dumps({'scope_declaration':{'files':[{'path':'app.py'}]}})
    emit('tool.result',{'text':text});emit('scope_declaration',{'files':[{'path':'app.py','accepted':True}]})
    assert not scope.declarations(store,rid)
    declare(emit,'../outside','src/*')
    assert len(scope.declarations(store,rid)[0]['payload']['errors'])==2
    assert not scope.declarations(store,rid)[0]['payload']['files']


def test_rename_and_deleted_paths_are_checked(env):
    root,store,p,rid,emit=env
    declare(emit,'moved.py');(root/'app.py').rename(root/'moved.py')
    assert check(env)['undeclared_changes']==['app.py']


def test_git_failure_and_node_recent_history(env,monkeypatch):
    root,store,p,rid,emit=env
    declare(emit,'app.py');(root/'app.py').write_text('x=2');item=check(env)
    store.update(rid,{'artifacts':{'scope_reconciliation':[item]}})
    rows=scope.recent(store,p['id'],{'code':[{'path':'app.py'}]})
    assert rows[0]['run_id']==rid and rows[0]['status']=='pass'
    assert scope.recent(store,p['id'],{'code':[{'path':'unrelated.py'}]})==[]
    monkeypatch.setattr(scope,'changed',lambda *a: (_ for _ in ()).throw(scope.SpecError('timeout')))
    assert scope.evidence(store,rid,p,root,'HEAD')[0]['status']=='unverified'


@pytest.mark.parametrize('declared',[True,False])
def test_independent_verifier_enforces_scope_without_drift(app_env,monkeypatch,declared):
    import threading
    from factory.control.execution import ExecutionError
    from factory.control.providers import ProviderResult
    from tests.test_control_app import login, project
    from tests.review_helpers import passing_review
    client,store,service,root=app_env
    p=project(client,root,login(client))
    p=store.update_project(p['id'],{'spec_tree_enabled':True},p['revision'],'test')
    from tests.test_spec_tree import document
    (root/'.spec/sample/spec.md').write_text(document('.'))
    commit(root,'govern all files')
    rid=store.create_run(p['id'],'change')[0]['id']
    run=store.update(rid,{'context':{'commit_sha':command(root,'rev-parse','HEAD')}})
    emit=scope.wrap_emit(store,rid,SimpleNamespace(workspace=str(root),read_only=False,verification=False),
                        lambda kind,data:store.append(rid,kind,data,'worker'))
    if declared: declare(emit,'extra.py')
    (root/'extra.py').write_text('x=1')
    node=root/'.spec/sample/spec.md';node.write_text(node.read_text()+'\nextra added\n')
    sha=commit(root,'change governed file and spec')
    monkeypatch.setattr(service.runner,'run',lambda req,*a,**kw:ProviderResult(passing_review(req,'observed'),cost_usd=.01))
    service.cancels[rid]=threading.Event();artifacts={'commit':sha}
    if declared:
        service._independent_verify(rid,run,p,service.runtime_settings.get(),artifacts)
    else:
        with pytest.raises(ExecutionError):service._independent_verify(rid,run,p,service.runtime_settings.get(),artifacts)
    item=next(i for i in artifacts['acceptance_ledger']['items'] if i['id']=='scope:reconciliation')
    assert item['status']==('pass' if declared else 'fail')
    assert artifacts['verification']['verdict']==item['status']
    assert artifacts['scope_reconciliation']==[item]
    store.update(rid,{'artifacts':artifacts})
    if declared:
        # Root governs '.', so API derives this association from code, not the worker's node claim.
        url=f"/api/v2/projects/{p['id']}/spec-tree/node"
        rows=client.get(url,params={'path':'.spec/sample/spec.md'}).json()['scope_declarations']
        assert rows[0]['run_id']==rid and rows[0]['status']=='pass'


from tests.test_control_app import app_env


def test_archived_additions_and_resumed_run_order(env):
    root,store,p,rid,emit=env
    declare(emit,*[f'file-{i}.py' for i in range(300)])
    event=scope.declarations(store,rid)[0]
    assert len(event['payload']['files'])==300
    newer=store.create_run(p['id'],'newer')[0]
    store.append(newer['id'],'scope_declaration',{'files':[{'path':'app.py','accepted':True}]},'newer')
    declare(emit,'app.py')
    rows=scope.recent(store,p['id'],{'code':[{'path':'app.py'}]})
    assert [r['run_id'] for r in rows]==[rid,newer['id']]
    assert rows[0]['status']=='pending'


def test_read_only_dispatches_do_not_collect(env):
    root,store,p,rid,emit=env
    callback=lambda *a: None
    assert scope.wrap_emit(store,rid,SimpleNamespace(read_only=True,verification=False),callback) is callback
    assert scope.wrap_emit(store,rid,SimpleNamespace(read_only=False,verification=True),callback) is callback
