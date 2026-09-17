from tests.test_workbench_app import app_env
from tests.test_control_app import login
from factory.control.pack_runtime import environment_report, _schema_check

def test_invocation_listing_not_shadowed(app_env):
    client, *_ = app_env
    headers = login(client)
    r = client.get('/api/v4/capability-packs/invocations', headers=headers)
    assert r.status_code == 200, r.text
    assert 'tasks' in r.json()

def test_dependency_probe_does_not_execute_manifest_code(tmp_path):
    marker = __import__('pathlib').Path('/tmp/codex_pack_probe_' + __import__('uuid').uuid4().hex)
    dependency = "os;open(" + repr(str(marker)) + ",'w').write('probe')#"
    environment_report({'dependency_lock': {'python': '3', 'packages': [dependency]}})
    assert not marker.exists(), 'manifest dependency executed code in unsandboxed environment probe'

def test_schema_rejects_enum_violation():
    assert _schema_check({'status':'invented'}, {'type':'object','properties':{'status':{'type':'string','enum':['ok','failed']}}})
