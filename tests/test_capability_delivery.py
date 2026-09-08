import io
import json
import time
import zipfile

from tests.test_autonomous_service import control, _autonomous, _new_run


def test_verified_delivery_harvests_draft_and_exports_exact_version(control):
    client, store, service, runner, project, headers = control
    _autonomous(client, project, headers)
    rid = _new_run(client, project, headers)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = store.get(rid)
        if run.get('capability_candidate_id'):
            break
        time.sleep(0.02)
    assert run['status'] == 'ready_for_review'
    cid = run['capability_candidate_id']
    first = client.get(f'/api/v3/capabilities/{cid}').json()
    assert first['source_run_id'] == rid and first['status'] == 'draft'
    assert run['artifacts']['commit'] in first['instructions']
    assert service._capture_capability(rid)['id'] == cid
    fields = ('name', 'description', 'category', 'instructions', 'input_description',
              'output_description', 'acceptance', 'status')
    changed = {key: first[key] for key in fields}
    changed.update(name='Reusable greeting', expected_revision=1, status='ready')
    response = client.put(f'/api/v3/capabilities/{cid}', json=changed, headers=headers)
    assert response.status_code == 200, response.text

    exported = client.get(f'/api/v3/capabilities/{cid}/export?revision=1')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == {'agent.json', 'SKILL.md', 'README.md', 'source-run.json'}
        manifest = json.loads(archive.read('agent.json'))
        assert manifest['capability']['revision'] == 1
        assert manifest['capability']['status'] == 'draft'
        assert json.loads(archive.read('source-run.json'))['id'] == rid
        assert first['name'] in archive.read('SKILL.md').decode()
    latest = client.get(f'/api/v3/capabilities/{cid}/export?format=json').json()
    assert latest['capability']['revision'] == 2
    assert client.get(f'/api/v3/capabilities/{cid}/export?revision=999').status_code == 404
    assert client.get(f'/api/v3/capabilities/{cid}/export?format=exe').status_code == 400
    assert client.get('/api/v3/environment').json()['mode'] == 'live'
    client.cookies.clear()
    assert client.get(f'/api/v3/capabilities/{cid}/export').status_code == 401
