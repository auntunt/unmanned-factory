import subprocess
from tests.test_control_app import login
from tests.test_workbench_app import app_env


def git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


def test_discovery_connect_infers_git_fields_without_accepting_paths(app_env):
    client, store, _, repo = app_env
    assert client.get('/api/v2/project-candidates').status_code == 401
    headers = login(client)
    git(repo, 'branch', '-m', 'maintenance')
    git(repo, 'remote', 'add', 'origin', 'https://secret-user:secret-token@github.com/acme/converter.git')
    result = client.get('/api/v2/project-candidates')
    assert result.status_code == 200
    assert 'secret-token' not in result.text and str(repo) not in result.text
    c, = result.json()['candidates']
    assert c['repository'] == 'acme/converter' and c['base_branch'] == 'maintenance'
    body = {'candidate_id': c['id'], 'name': '转换器维护'}
    assert client.post('/api/v2/projects/connect', json={**body, 'workspace': '/tmp/anything'}, headers=headers).status_code == 422
    assert client.post('/api/v2/projects/connect', json=body).status_code == 403
    response = client.post('/api/v2/projects/connect', json=body, headers=headers)
    assert response.status_code == 201, response.text
    p = response.json()
    assert p['workspace'] == str(repo) and p['base_branch'] == 'maintenance'
    assert p['name'] == '转换器维护'
    assert client.get('/api/v2/project-candidates').json()['candidates'][0]['project_id'] == p['id']
    assert client.post('/api/v2/projects/connect', json=body, headers=headers).status_code == 409
    assert len(store.projects()) == 1


def test_discovery_local_stale_and_outside_symlink(app_env):
    client, _, _, repo = app_env
    headers = login(client)
    outside = repo.parent.parent / 'outside'
    outside.mkdir()
    (repo.parent / 'escape').symlink_to(outside, target_is_directory=True)
    candidates = client.get('/api/v2/project-candidates').json()['candidates']
    assert len(candidates) == 1
    c = candidates[0]
    assert c['repository'].startswith('local/')
    assert client.post('/api/v2/projects/connect', json={'candidate_id': c['id'], 'auto_publish': True}, headers=headers).status_code == 400
    git(repo, 'branch', '-m', 'renamed')
    assert client.post('/api/v2/projects/connect', json={'candidate_id': c['id']}, headers=headers).status_code == 409
    c = client.get('/api/v2/project-candidates').json()['candidates'][0]
    assert client.post('/api/v2/projects/connect', json={'candidate_id': c['id']}, headers=headers).status_code == 201


def test_html_and_missing_assets_are_not_cached(app_env):
    client, _, _, _ = app_env
    response = client.get('/')
    if response.status_code == 200:
        assert response.headers['cache-control'] == 'no-store'
    missing = client.get('/assets/retired-build.js')
    assert missing.status_code == 404 and missing.headers['cache-control'] == 'no-store'


def test_configured_static_directory_is_the_actual_page_source(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from factory.control.app import create_app
    static = tmp_path / 'reviewed-build'
    static.mkdir()
    (static / 'index.html').write_text('<html>reviewed-build-marker</html>')
    monkeypatch.setenv('FACTORY_STATIC_DIR', str(static))
    with TestClient(create_app(data_dir=tmp_path / 'data', workspace_root=tmp_path / 'projects', public_origin='http://testserver')) as client:
        response = client.get('/projects')
        assert response.status_code == 200 and 'reviewed-build-marker' in response.text
        assert response.headers['cache-control'] == 'no-store'


def test_stale_runtime_clients_get_actionable_errors(app_env):
    client, _, _, _ = app_env
    headers = login(client)
    old = client.get('/api/runtime')
    assert old.status_code == 404 and '旧版' in old.json()['detail']
    missing = client.post('/api/v2/runtime/probe', json={'profile': 'planner'}, headers=headers)
    assert missing.status_code == 422 and '刷新页面' in missing.json()['detail']
