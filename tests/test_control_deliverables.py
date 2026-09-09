import io
import json
import shutil
import subprocess
import zipfile

import httpx
import pytest

from factory.control.deliverables import snapshot
from factory.control.github import publish_failure_message
from factory.control.store import Conflict, Store
from tests.test_control_app import app_env, login, project, wait_state


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True).stdout.decode().strip()


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    git(root, 'init', '-q'); git(root, 'config', 'user.name', 'Test'); git(root, 'config', 'user.email', 'test@example.com')
    (root / 'README.md').write_text('使用说明')
    (root / 'pelican.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>')
    (root / '.env').write_text('PRIVATE=secret')
    (root / '.gitignore').write_text('dist/\n.env\n')
    (root / 'dist').mkdir(); (root / 'dist' / 'app.dmg').write_bytes(b'installer bytes')
    (root / 'dist' / 'index.html').write_text('<h1>Preview</h1>')
    (root / 'dist' / 'escape').symlink_to(root / '.env')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'delivery')
    store = Store(tmp_path / 'data' / 'control.db')
    run = {'id': 'run1', 'status': 'ready_for_review', 'artifacts': {'worktree': str(root), 'commit': git(root, 'rev-parse', 'HEAD')}}
    return store, run, root


def test_snapshot_source_installer_and_web_excludes_secrets_and_symlinks(delivery):
    store, run, root = delivery
    manifest = snapshot(store, run)
    files = {i['name']: i for i in manifest['items']}
    assert 'README.md' in files
    assert files['pelican.svg']['kind'] == 'image' and files['pelican.svg']['preview']
    assert files['dist/app.dmg']['kind'] == 'installer'
    assert files['dist/index.html']['preview']
    assert '.env' not in files and 'dist/escape' not in files
    assert files['README.md']['origin'] == 'commit'
    assert files['dist/app.dmg']['origin'] == 'build'
    (root / 'dist' / 'app.dmg').write_bytes(b'changed later')
    assert snapshot(store, run) == manifest
    path = root.parent / 'data' / 'deliverables' / 'run1' / run['artifacts']['commit'] / 'files.zip'
    with zipfile.ZipFile(path) as archive:
        assert archive.read('dist/app.dmg') == b'installer bytes'


def test_reject_modified_source(delivery):
    store, run, root = delivery
    (root / 'README.md').write_text('modified after verification')
    with pytest.raises(Conflict, match='代码已变化'): snapshot(store, run)


def test_explicit_manifest_selects_untracked_outputs(delivery):
    store, run, root = delivery
    (root / '.factory-delivery.json').write_text(json.dumps({'files': ['dist/app.dmg']}))
    git(root, 'add', '.factory-delivery.json'); git(root, 'commit', '-qm', 'manifest')
    run['artifacts']['commit'] = git(root, 'rev-parse', 'HEAD')
    names = {i['name'] for i in snapshot(store, run)['items']}
    assert 'dist/app.dmg' in names and 'dist/index.html' not in names


def test_reject_traversal_manifest(delivery):
    store, run, root = delivery
    (root / '.factory-delivery.json').write_text('{"files": ["../private.key"]}')
    git(root, 'add', '.factory-delivery.json'); git(root, 'commit', '-qm', 'manifest')
    run['artifacts']['commit'] = git(root, 'rev-parse', 'HEAD')
    with pytest.raises(Conflict): snapshot(store, run)


def test_publish_diagnostics_hide_request_credentials():
    req = httpx.Request('POST', 'https://secret:password@api.github.com/repos/x/y/pulls')
    exc = httpx.HTTPStatusError('secret credential', request=req, response=httpx.Response(403, request=req))
    message = publish_failure_message(exc)
    assert '权限' in message and 'secret' not in message and 'password' not in message


def test_http_download_survives_worktree_removal_and_github_failure(app_env, caplog):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    from factory.control.autonomy import DEFAULT_POLICY
    svc.policies.update(pid, {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'test')
    response = client.post('/api/v2/runs', headers=headers, json={'project_id': pid, 'request': 'Update greeting to hello world'})
    rid = response.json()['id']
    planned = wait_state(store, rid, {'awaiting_approval'})
    client.post(f'/api/v2/runs/{rid}/approve', headers=headers, json={'revision': planned['revision']})
    run = wait_state(store, rid, {'ready_for_review', 'failed'})
    assert run['status'] == 'ready_for_review'
    base = f'/api/v3/runs/{rid}/deliverables'
    catalog = client.get(base).json()
    assert catalog['saved'] and not catalog['github_configured']
    item = next(i for i in catalog['items'] if i['name'] == 'greeting.txt')
    assert client.get(f'{base}/files/{item["id"]}?preview=true').json()['content'] == 'hello world'
    # A pre-upgrade run can collect through the authenticated UI action.
    archive_root = __import__('pathlib').Path(store.path).parent / 'deliverables' / rid
    shutil.rmtree(archive_root)
    assert client.get(base).json()['saved'] is False
    assert client.post(f'{base}/collect').status_code == 403
    assert client.post(f'{base}/collect', headers=headers).status_code == 200
    assert client.get(base).json()['saved'] is True
    class BrokenPublisher:
        def publish(self, project, run):
            raise httpx.ConnectError('private network details')
        def close(self): pass
    svc.publisher = BrokenPublisher()
    response = client.post(f'/api/v2/runs/{rid}/publish', headers=headers)
    assert response.status_code == 502 and '服务器网络' in response.json()['detail']
    assert store.get(rid)['status'] == 'ready_for_review'
    assert rid in caplog.text and 'ConnectError' in caplog.text
    assert 'private network details' not in caplog.text
    assert '服务器网络' in client.get(base).json()['publish_error']
    shutil.rmtree(run['artifacts']['worktree'])
    response = client.get(f'{base}/download')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('greeting.txt') == b'hello world'
    assert client.get(f'{base}/files/999').status_code == 404
    client.cookies.clear()
    assert client.get(f'{base}/download').status_code == 401


def test_auto_publish_failure_preserves_verified_delivery(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    store.update_project(pid, {'auto_publish': True}, 1, 'test')
    class BrokenPublisher:
        def publish(self, project, run):
            raise httpx.ConnectError('unavailable')
        def close(self): pass
    svc.publisher = BrokenPublisher()
    rid = client.post('/api/v2/runs', headers=headers, json={'project_id': pid, 'request': 'Update greeting to hello world'}).json()['id']
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(e['type'] == 'github.publish_failed' for e in store.events(rid)):
            break
        time.sleep(0.02)
    else:
        pytest.fail('automatic publication did not finish')
    for future in list(svc.futures): future.result(timeout=5)
    assert store.get(rid)['status'] == 'ready_for_review'
    assert client.get(f'/api/v3/runs/{rid}/deliverables/download').status_code == 200


def test_svg_preview_is_image_data_and_download_is_attachment(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    from factory.control.autonomy import DEFAULT_POLICY
    svc.policies.update(pid, {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'test')
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><circle r="10"/></svg>'
    (repo / 'pelican.svg').write_text(svg)
    git(repo, 'add', 'pelican.svg'); git(repo, 'commit', '-qm', 'svg')
    rid = client.post('/api/v2/runs', headers=headers, json={'project_id': pid, 'request': 'Update greeting to hello world'}).json()['id']
    run = wait_state(store, rid, {'awaiting_approval'})
    client.post(f'/api/v2/runs/{rid}/approve', headers=headers, json={'revision': run['revision']})
    assert wait_state(store, rid, {'ready_for_review', 'failed'})['status'] == 'ready_for_review'
    base = f'/api/v3/runs/{rid}/deliverables'
    item = next(i for i in client.get(base).json()['items'] if i['name'] == 'pelican.svg')
    response = client.get(f'{base}/files/{item["id"]}?preview=true')
    assert response.headers['content-type'] == 'application/json'
    assert response.json()['image_url'].startswith('data:image/svg+xml;base64,')
    response = client.get(f'{base}/files/{item["id"]}')
    assert response.text == svg
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.parametrize(('raw', 'expected'), [
    ('fatal: Authentication failed for https://secret@github.com', '凭据无效'),
    ('remote: Write access to repository not granted', '写入权限'),
    ('remote: Repository not found', '仓库不存在'),
    ('rejected: non-fast-forward', '远端分支已经变化'),
    ('remote: GH013: Repository rule violations', '仓库规则'),
    ('fatal: Could not resolve host github.com', '网络'),
])
def test_git_push_errors_are_actionable_and_sanitized(raw, expected):
    from factory.control.github import push_failure_message
    message = push_failure_message(raw)
    assert expected in message and 'secret' not in message


def test_oversized_artifact_does_not_leave_partial_archive(delivery, monkeypatch):
    store, run, root = delivery
    monkeypatch.setattr('factory.control.deliverables.MAX_TOTAL', 8)
    with pytest.raises(Conflict, match='归档上限'):
        snapshot(store, run)
    path = root.parent / 'data' / 'deliverables' / run['id'] / run['artifacts']['commit']
    assert not path.exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../../escape', 'a\\..\\escape', 'C:/private', '.env', 'dist/.env.production'])
def test_reject_unsafe_archive_paths(name):
    from factory.control.deliverables import safe_path
    assert not safe_path(name)


@pytest.mark.parametrize('sha', [None, '', 123, {}, 'invalid'])
def test_missing_commit_is_not_a_server_error(app_env, sha):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    run, _ = store.create_run(pid, 'paused delivery', source={'type': 'test'})
    store.update(run['id'], {'status': 'needs_human', 'artifacts': {'commit': sha, 'billing_incomplete': 'unknown cost'}})
    response = client.get(f"/api/v3/runs/{run['id']}/deliverables")
    assert response.status_code == 200
    assert response.json()['items'] == []
    assert response.json()['can_collect'] is False
    assert client.get(f"/api/v3/runs/{run['id']}/deliverables/download").status_code == 404
    with pytest.raises(Conflict, match='验收版本'):
        snapshot(store, store.get(run['id']))


def test_paused_unknown_cost_can_resume_with_current_bounded_policy(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    run, _ = store.create_run(pid, 'resume delivery', source={'type': 'test'})
    current = svc.runtime_settings.get()
    frozen = {**current, 'limits': {**current['limits'], 'unknown_cost_policy': 'stop', 'max_tasks': 3}}
    from factory.control.codegraph import baseline_sha
    paused = store.update(run['id'], {
        'status': 'needs_human', 'plan': {'tasks': []},
        'runtime_configuration': frozen,
        'artifacts': {'base_sha': baseline_sha(store.project(pid)), 'tasks': [{'id': 'first'}]},
    })
    monkeypatch.setattr(svc, '_usage', lambda rid: {'known_cost_usd': 0, 'unknown_cost_calls': 1})
    monkeypatch.setattr(svc, '_submit', lambda *args: None)
    resumed = svc.continue_run(run['id'], '继续完成交付', paused['revision'], 0, 'owner')
    assert resumed['status'] == 'queued'
    config = resumed['runtime_configuration']
    assert config['limits']['unknown_cost_policy'] == 'allow_bounded'
    assert config['limits']['max_tasks'] == 3
    assert config['profiles'] == frozen['profiles']
    assert resumed['execution_resume']['artifacts'] == paused['artifacts']


@pytest.mark.parametrize('stop_policy,budget_used', [(True, False), (False, True)])
def test_continuation_retains_explicit_cost_and_budget_limits(app_env, monkeypatch, stop_policy, budget_used):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    run, _ = store.create_run(pid, 'resume delivery', source={'type': 'test'})
    current = svc.runtime_settings.get()
    frozen = {**current, 'limits': {**current['limits'], 'unknown_cost_policy': 'stop', 'max_tasks': 3}}
    from factory.control.codegraph import baseline_sha
    paused = store.update(run['id'], {
        'status': 'needs_human', 'plan': {'tasks': []},
        'runtime_configuration': frozen,
        'artifacts': {'base_sha': baseline_sha(store.project(pid)), 'tasks': [{'id': 'first'}]},
    })
    monkeypatch.setattr(svc, '_usage', lambda rid: {'known_cost_usd': 0, 'unknown_cost_calls': 1})
    monkeypatch.setattr(svc, '_submit', lambda *args: None)
    if stop_policy:
        svc.runtime_settings.update({'profiles': current['profiles'], 'limits': {**current['limits'], 'unknown_cost_policy': 'stop'}}, current['revision'], 'test')
    if budget_used:
        monkeypatch.setattr(svc, '_usage', lambda rid: {'known_cost_usd': store.project(pid)['budget_usd'], 'unknown_cost_calls': 1})
    with pytest.raises(Conflict):
        svc.continue_run(run['id'], '继续完成交付', paused['revision'], 0, 'owner')
    assert store.get(run['id'])['status'] == 'needs_human'
