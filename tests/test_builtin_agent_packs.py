import io
import json
import zipfile

from factory.control.agent_packs import catalog, configuration, install_builtins, pack_source, pack_zip
from factory.control.agents import AgentStore, inspect_skill
from factory.control.modules import ModuleStore
from factory.control.store import Store
from tests.test_control_app import login
from tests.test_workbench_app import app_env


def test_each_pack_is_reproducible_uploadable_and_complete():
    assert len(catalog()) == 4
    for pack in catalog():
        raw = pack_zip(pack['id'])
        assert raw == pack_zip(pack['id'])
        meta = inspect_skill(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            manifest = json.loads(z.read('agent.json'))
            cases = json.loads(z.read('evaluations.json'))
            assert manifest['configuration']['model_settings'] == {}
            assert manifest['configuration']['tool_scope'] == []
            assert len(cases) >= 2 and all(c['expected'] and c['failure_probe'] for c in cases)
            for case in cases:
                if 'fixture' in case:
                    assert case['fixture'] in z.namelist()
            for m in manifest['module_sources']:
                module = json.loads(z.read('modules/' + m['id'] + '.json'))
                assert module['instructions'] in manifest['configuration']['instructions']
                assert module['instructions'] in z.read('SKILL.md').decode()
        assert meta['size_bytes'] == len(raw)


def test_install_is_idempotent_and_preserves_team_changes(tmp_path):
    store = Store(tmp_path / 'control.db')
    install_builtins(store)
    agents = AgentStore(store)
    assert len(agents.list()) == 4
    a = agents.list()[0]
    v = agents.version(a['id'])
    draft = agents.save_draft(a['id'], {'instructions': v['instructions'] + '\n团队自有方法'}, 0)
    agents.apply(a['id'], draft['revision'], 'test-builtin-update')
    modules = ModuleStore(store)
    m = next(m for m in modules.list() if m['id'] == 'builtin-code-evidence')
    modules.save({**m, 'instructions': '团队修改后的模块'}, 'tester', m['id'], 1)
    install_builtins(store)
    assert len(agents.list()) == 4
    assert agents.version(a['id'])['version'] == 2
    assert agents.version(a['id'])['instructions'].endswith('团队自有方法')
    assert next(m for m in modules.list() if m['id'] == 'builtin-code-evidence')['version'] == 2
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM skill_assets').fetchone()[0] == 4
    for agent in agents.list():
        version = agents.version(agent['id'])
        assert inspect_skill(agents.skill_body(version['skill_ids'][0], agent_id=agent['id']))


def test_download_authentication_and_upload_round_trip(app_env):
    client, store, service, repo = app_env
    route = '/api/v4/builtin-packs/automation-cli/download'
    assert client.get(route).status_code == 401
    headers = login(client)
    response = client.get(route)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert client.get('/api/v4/builtin-packs/not-a-pack/download').status_code == 404
    a = client.post('/api/v4/agents', headers=headers, json={'name': '包导入验收'}).json()
    uploaded = client.post(f"/api/v4/agents/{a['id']}/skills", headers=headers,
        files={'file': ('automation-cli.zip', response.content, 'application/zip')})
    assert uploaded.status_code == 201, uploaded.text
    assert AgentStore(store).skill_body(uploaded.json()['id'], agent_id=a['id']) == response.content
