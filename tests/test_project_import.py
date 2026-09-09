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


def test_imported_cli_is_maintained_and_functionally_verified(app_env, monkeypatch):
    import sys
    from factory.control.providers import ProviderResult
    from tests.test_control_app import wait_state
    client, store, service, _ = app_env
    headers = login(client)
    response = post(client, headers, archive([('cli/main.py', 'import sys\nprint(int(sys.argv[1]) * 2)\n'),
        ('cli/README.md', 'CLI: python main.py NUMBER. Multiply input by two.')]))
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
            return ProviderResult(json.dumps({'verdict': 'pass' if output == '21' else 'fail', 'reason': '实际运行 CLI 并核对输出'}), cost_usd=.01)
        if request.read_only:
            return ProviderResult(json.dumps({'title': '修改倍数', 'summary': '将转换倍数改为三', 'questions': [], 'tasks': [
                {'id': 'cli', 'title': '维护 CLI', 'prompt': '将 main.py 倍数改成三', 'acceptance': ['输入 7 输出 21'],
                 'paths': ['main.py'], 'checks': ['cli-result'], 'depends_on': [], 'complexity': 'small', 'risk': 'low'}]}), cost_usd=.01)
        report = json.loads((root / '.webuddy/import-report.json').read_text())
        assert report['entrypoints'] == ['main.py']
        (root / 'main.py').write_text('import sys\nprint(int(sys.argv[1]) * 3)\n')
        return ProviderResult('倍数已修改并准备验证', cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', runner)
    response = client.post('/api/v2/runs', json={'project_id': project['id'], 'request': '把计算倍数改成三'}, headers=headers)
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
    assert post(client, headers, payload, idempotency_key='oversized-import').status_code == 400
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
