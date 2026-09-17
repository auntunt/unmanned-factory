from tests.test_workbench_app import app_env
from tests.test_control_app import login
from tests.test_capability_packs import development_run, selections, key
from factory.control.capability_packs import PackStore
from factory.control.pack_runtime import validate_instance

def test_eval_registered_before_dispatch_is_not_false_completed(app_env):
    client,store,service,repo=app_env
    headers=login(client)
    rid=development_run(client,store,repo,headers)
    pack=client.post('/api/v4/capability-packs/drafts',headers=headers,json={'source_run_id':rid,'selections':selections(),'operation_key':key()}).json()
    actor=client.get('/api/auth/me',headers=headers).json()['user']
    op=key()
    PackStore(store).begin_evaluation(pack['id'],content_digest_value=pack['draft']['content_digest'],actor=actor,operation_key=op,job_id=key())
    replay=client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations",headers=headers,json={'expected_revision':pack['draft']['revision'],'operation_key':op})
    assert replay.status_code==202
    data=replay.json()
    assert data['status']!='completed', data
    assert client.get('/api/v4/capability-packs/jobs/'+data['job_id'],headers=headers).status_code==200

def test_bad_local_ref_is_structured_failure():
    result=validate_instance({}, {'$ref':'#/$defs/missing'},where='input')
    assert result
