# Legacy execution assertions explicitly use maintenance; default general confirmation is covered in test_requirement_analysis.py.
import io
import json
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from factory.control import project_import
from tests.test_workbench_app import app_env
from tests.test_control_app import login


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files:
            z.writestr(name, content)
    return output.getvalue()


def post(client, headers, payload, **fields):
    return client.post('/api/v2/projects/import-zip', headers=headers,
        data={'name': '转换助手', 'idempotency_key': 'import-project-one', **fields},
        files={'file': ('project.zip', payload, 'application/zip')})


def test_import_retains_baseline_report_original_and_retries(app_env):
    client, store, svc, repo = app_env
    payload = archive([('tool/README.md', 'Convert files'), ('tool/pyproject.toml', '[project]\nname="converter"'),
        ('tool/main.py', 'raise RuntimeError("must not execute")'), ('tool/.env', 'SECRET=never-load'),
        ('tool/.env.example', 'SECRET='), ('tool/.git/config', '[core]\nhooksPath=/tmp/evil')])
    headers = login(client)
    result = post(client, headers, payload)
    assert result.status_code == 201, result.text
    p = result.json()['project']; summary = result.json()['import_summary']; root = Path(p['workspace'])
    assert summary['stripped_root'] == 'tool'
    assert summary['baseline_status'] == 'not_run'
    assert summary['entrypoints'] == ['main.py'] and summary['manifests'] == ['pyproject.toml']
    assert not (root / '.env').exists() and (root / '.env.example').exists()
    assert json.loads((root / summary['report_path']).read_text()) == summary
    assert (Path(p['import_evidence']) / 'original.zip').read_bytes() == payload
    tracked = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', 'HEAD'], cwd=root).decode()
    assert 'main.py' in tracked and '.webuddy/import-report.json' in tracked
    assert subprocess.check_output(['git', 'status', '--porcelain'], cwd=root) == b''
    assert len(store.all_runs()) == 0
    assert post(client, headers, payload).json()['project']['id'] == p['id']
    assert post(client, headers, payload, name='changed').status_code == 409
    assert len(store.projects()) == 1


@pytest.mark.parametrize('files', [
    [('../escape.py', 'x')], [('/absolute.py', 'x')], [('C:/file.py', 'x')], [('a\\b.py', 'x')],
    [('a.py', 'x'), ('A.py', 'y')], [('dir', 'x'), ('dir/file.py', 'y')],
    [('x/./file', 'x')], [('x/../file', 'x')], [('main.py', 'a' * (2 * 1024 * 1024))],
])
def test_rejects_unsafe_paths_collisions_and_bombs_without_project(app_env, files):
    client, store, _, repo = app_env
    result = post(client, login(client), archive(files))
    assert result.status_code == 400, result.text
    assert not store.projects() and not list(repo.parent.glob('workspace-*'))


def test_rejects_symlink_corrupt_and_too_many_entries(app_env, monkeypatch):
    client, store, _, _ = app_env
    headers = login(client)
    link = zipfile.ZipInfo('link'); link.create_system = 3; link.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert post(client, headers, archive([(link, '/tmp/secret')])).status_code == 400
    assert post(client, headers, b'invalid').status_code == 400
    monkeypatch.setattr(project_import, 'MAX_FILES', 1)
    assert post(client, headers, archive([('a', 'x'), ('b', 'x')])).status_code == 400
    assert not store.projects()


def test_upload_auth_csrf_and_member_boundary(app_env):
    client, store, _, _ = app_env
    payload = archive([('main.py', 'pass')])
    assert post(client, {'origin': 'http://testserver'}, payload).status_code == 401
    headers = login(client)
    assert post(client, {}, payload).status_code == 403
    assert post(client, {'origin': headers['Origin']}, payload).status_code == 403
    client.app.state.auth.create_user('member', 'member-password-123', role='member')
    client.post('/api/auth/logout', headers=headers)
    response = client.post('/api/auth/login', headers={'origin': headers['Origin']}, json={'username': 'member', 'password': 'member-password-123'})
    member_headers = {'origin': headers['Origin'], 'x-csrf-token': response.json()['csrf_token']}
    assert post(client, member_headers, payload).status_code == 403
    assert not store.projects()


def test_global_git_clean_filters_do_not_execute(app_env, monkeypatch, tmp_path):
    client, _, _, _ = app_env
    marker = tmp_path / 'executed'
    config = tmp_path / 'gitconfig'
    config.write_text('[filter "evil"]\n clean = touch ' + str(marker) + '\n required = true\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    response = post(client, login(client), archive([('.gitattributes', '*.py filter=evil'), ('main.py', 'pass')]))
    assert response.status_code == 201, response.text
    assert not marker.exists()


def test_import_rollback_on_repository_failure(app_env, monkeypatch):
    client, store, _, repo = app_env
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'git')
    monkeypatch.setattr(project_import, 'initialize_repository', fail)
    response = post(client, login(client), archive([('main.py', 'pass')]))
    assert response.status_code == 503
    assert not store.projects() and not list(repo.parent.glob('workspace-*'))


@pytest.mark.parametrize('root', ['.git', '.webuddy', 'node_modules', '.env'])
def test_reserved_only_archive_cannot_be_stripped_into_project(app_env, root):
    client, store, _, _ = app_env
    assert post(client, login(client), archive([(root + '/config', 'x')])).status_code == 400
    assert not store.projects()


def test_mac_metadata_does_not_prevent_wrapper_detection(app_env):
    client, _, _, _ = app_env
    response = post(client, login(client), archive([('tool/main.py', 'pass'), ('__MACOSX/._tool', 'metadata')]))
    assert response.status_code == 201
    assert response.json()['import_summary']['stripped_root'] == 'tool'
    assert response.json()['import_summary']['file_count'] == 1


@pytest.mark.parametrize('input_kind', ['zip', 'files'])
def test_imported_cli_is_maintained_and_functionally_verified(app_env, monkeypatch, input_kind):
    import sys
    from factory.control.providers import ProviderResult
    from tests.test_control_app import wait_state
    client, store, service, _ = app_env
    headers = login(client)
    files = [('main.py', 'import sys\nprint(int(sys.argv[1]) * 2)\n'),
             ('README.md', 'CLI: python main.py NUMBER. Multiply input by two.')]
    response = (post(client, headers, archive([('cli/' + name, body) for name, body in files]))
        if input_kind == 'zip' else client.post('/api/v2/projects/import-files', headers=headers,
            data={'name': '转换项目', 'idempotency_key': 'functional-files'},
            files=[('files', (name, body.encode())) for name, body in files]))
    assert response.status_code == 201, response.text
    project = response.json()['project']
    # Explicitly configure a genuine functional assertion as the maintenance contract.
    check = [sys.executable, '-c', "import subprocess,sys; assert subprocess.check_output([sys.executable,'main.py','7'],text=True).strip() == '21'"]
    store.update_project(project['id'], {'checks': {'cli-result': check}}, project['revision'], 'test')
    observed = []
    def runner(request, emit, cancel=None):
        root = Path(request.workspace)
        if request.read_only and 'TASK ACCEPTANCE:' in request.prompt:
            output = subprocess.check_output([sys.executable, 'main.py', '7'], cwd=root, text=True).strip()
            observed.append(output)
            from tests.review_helpers import passing_review
            assert output == '21'
            return ProviderResult(passing_review(request, '实际运行 CLI 并核对输出为 21'), cost_usd=.01)
        if request.read_only:
            return ProviderResult(json.dumps({'title': '修改倍数', 'summary': '将转换倍数改为三', 'questions': [], 'tasks': [
                {'id': 'cli', 'title': '维护 CLI', 'prompt': '将 main.py 倍数改成三', 'acceptance': ['输入 7 输出 21'],
                 'paths': ['main.py'], 'checks': ['cli-result'], 'depends_on': [], 'complexity': 'small', 'risk': 'low'}]}), cost_usd=.01)
        report = json.loads((root / '.webuddy/import-report.json').read_text())
        assert report['entrypoints'] == ['main.py']
        (root / 'main.py').write_text('import sys\nprint(int(sys.argv[1]) * 3)\n')
        return ProviderResult('倍数已修改并准备验证', cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', runner)
    response = client.post('/api/v2/runs', json={'operation': 'bugfix', 'project_id': project['id'], 'request': '把计算倍数改成三'}, headers=headers)
    assert response.status_code == 201, response.text
    run = wait_state(store, response.json()['id'], {'ready_for_review', 'failed', 'needs_human', 'needs_clarification'})
    assert run['status'] == 'ready_for_review', (run, store.events(run['id']))
    assert observed == ['21']
    assert any(c['name'] == 'cli-result' and c['exit'] == 0 for c in run['artifacts']['checks'])
    deliverables = client.get(f"/api/v3/runs/{run['id']}/deliverables").json()
    assert deliverables['saved'] and any(item['name'] == 'main.py' for item in deliverables['items'])
    assert (Path(project['import_evidence']) / 'original.zip').exists()


def test_archive_limit_and_multipart_stream_over_default_json_limit(app_env, monkeypatch):
    import os
    client, store, _, _ = app_env
    headers = login(client)
    payload = archive([('sample.bin', os.urandom(1100000))])
    assert post(client, headers, payload).status_code == 201
    monkeypatch.setattr(project_import, 'MAX_ARCHIVE', 50)
    assert post(client, headers, payload, idempotency_key='oversized-import').status_code == 413
    assert len(store.projects()) == 1


def test_optional_agent_binding_and_unknown_agent_do_not_create_project(app_env):
    from tests.test_project_assistants import setup_project
    client, store, _, headers, _, helpers, agent = setup_project(app_env)
    payload = archive([('main.py', 'pass')])
    response = post(client, headers, payload, agent_id=agent['id'])
    assert response.status_code == 201, response.text
    pid = response.json()['project']['id']
    assert helpers.binding(pid)['agent_id'] == agent['id']
    assert post(client, headers, payload, agent_id=agent['id']).json()['project']['id'] == pid
    assert helpers.binding(pid)['revision'] == 1
    count = len(store.projects())
    assert post(client, headers, payload, agent_id='missing', idempotency_key='missing-agent').status_code == 404
    assert len(store.projects()) == count


def test_concurrent_import_retry_creates_single_baseline(app_env):
    from concurrent.futures import ThreadPoolExecutor
    _, store, _, repo = app_env
    payload = archive([('main.py', 'pass')])
    def create(_):
        return project_import.import_project(store, repo.parent, io.BytesIO(payload), filename='project.zip',
            name='并发导入', budget_usd=10, actor_id=1, idempotency_key='concurrent-import')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert results[0]['project']['id'] == results[1]['project']['id']
    assert len(store.projects()) == 1 and len(list(repo.parent.glob('workspace-*'))) == 1


def test_sample_files_are_byte_exact_tracked_and_agent_bound(app_env):
    from tests.test_project_assistants import setup_project
    client, store, service, headers, _, helpers, agent = setup_project(app_env)
    files = [('files', ('sample.custom', b'\x00\xff\x01format')),
             ('files', ('notes.txt', '只做格式转换'.encode())),
             ('files', ('never-run.sh', b'exit 42'))]
    data = {'name': '格式转换资料', 'idempotency_key': 'samples-one', 'agent_id': agent['id']}
    response = client.post('/api/v2/projects/import-files', headers=headers, data=data, files=files)
    assert response.status_code == 201, response.text
    p = response.json()['project']; root = Path(p['workspace'])
    for _, (name, raw) in files:
        assert (root / name).read_bytes() == raw
    assert helpers.binding(p['id'])['agent_id'] == agent['id']
    assert p['budget_usd'] is None
    assert not store.all_runs()  # Upload never runs code or a model.
    assert subprocess.check_output(['git', 'show', 'HEAD:sample.custom'], cwd=root) == b'\x00\xff\x01format'
    again = client.post('/api/v2/projects/import-files', headers=headers, data=data, files=files)
    assert again.json()['project']['id'] == p['id']
    changed = client.post('/api/v2/projects/import-files', headers=headers, data=data,
                         files=[('files', ('sample.custom', b'changed'))])
    assert changed.status_code == 409


@pytest.mark.parametrize('names', [['../escape'], ['.env'], ['A.bin', 'a.bin']])
def test_sample_files_reject_unsafe_or_conflicting_names(app_env, names):
    client, store, _, _ = app_env
    response = client.post('/api/v2/projects/import-files', headers=login(client),
        data={'name': '资料', 'idempotency_key': 'reject-samples'},
        files=[('files', (name, b'data')) for name in names])
    assert response.status_code == 400, response.text
    assert not store.projects()


def test_sample_files_authorization_and_size_limit(app_env, monkeypatch):
    client, store, _, _ = app_env
    data = {'name': '资料', 'idempotency_key': 'auth-samples'}
    files = [('files', ('sample.bin', b'data'))]
    assert client.post('/api/v2/projects/import-files', data=data, files=files,
                       headers={'Origin': 'http://testserver'}).status_code == 401
    headers = login(client)
    assert client.post('/api/v2/projects/import-files', data=data, files=files,
                       headers={'Origin': 'http://testserver'}).status_code == 403
    monkeypatch.setattr(project_import, 'MAX_ARCHIVE', 65538)
    assert client.post('/api/v2/projects/import-files', data=data, files=files, headers=headers).status_code == 400
    assert not store.projects()


def test_project_zip_over_old_upload_limit_is_imported(app_env):
    client, store, _, _ = app_env
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive_file:
        archive_file.writestr('sample.bin', b'x' * (22 * 1024 * 1024))
    response = post(client, login(client), output.getvalue())
    assert response.status_code == 201, response.text
    assert response.json()['import_summary']['total_bytes'] == 22 * 1024 * 1024


def test_project_archive_one_gib_boundary_rejects_before_reading():
    assert project_import.MAX_ARCHIVE == 1024 ** 3
    class OversizedUpload:
        def seek(self, *args): pass
        def tell(self): return 1024 ** 3 + 1
        def read(self, *args): raise AssertionError('oversized body must not be read')
    with pytest.raises(project_import.ImportError, match='1 GB'):
        project_import.import_project(None, None, OversizedUpload(), filename='large.zip',
            name='large', budget_usd=None, actor_id=1, idempotency_key='oversized')


def test_expanded_limit_allows_large_projects_but_stays_bounded():
    class ArchiveMetadata:
        def __init__(self, size): self.size = size
        def infolist(self):
            item = zipfile.ZipInfo('sample.bin')
            item.file_size = item.compress_size = self.size
            return [item]
    assert project_import.MAX_EXPANDED == 2 * 1024 ** 3
    assert len(project_import._members(ArchiveMetadata(2 * 1024 ** 3))) == 1
    with pytest.raises(project_import.ImportError, match='展开体积'):
        project_import._members(ArchiveMetadata(2 * 1024 ** 3 + 1))


@pytest.mark.parametrize('content_length', [True, False])
def test_project_upload_stream_remains_bounded(app_env, monkeypatch, content_length):
    client, store, _, _ = app_env
    headers = login(client)
    monkeypatch.setattr(project_import, 'MAX_ARCHIVE', 1024)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive_file:
        archive_file.writestr('large.bin', b'x' * (2 * 1024 * 1024))
    request = client.build_request('POST', '/api/v2/projects/import-zip', headers=headers,
        data={'name': 'large', 'idempotency_key': 'too-large'},
        files={'file': ('project.zip', output.getvalue(), 'application/zip')})
    if not content_length:
        request.headers.pop('content-length', None)
    response = client.send(request)
    assert response.status_code == 413, response.text
    assert not store.projects()
