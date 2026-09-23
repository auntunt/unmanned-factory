"""Private GitHub repositories registered by URL: platform credential, retry, dedupe.

Only `git clone` is faked (no network): it records argv/env and then either
fails with a real git message or clones the local fixture repository. Every
other git call runs for real.
"""
import base64
import subprocess
import threading
import time

from factory.control import maintenance_subsystem as ms
from tests.test_control_app import app_env, login  # noqa: F401

URL = 'https://github.com/auntunt/group-risk-data-system.git'
TOKEN = 'ghp_synthetic_test_token_1234567890'
NO_AUTH = "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"


class FakeClone:
    def __init__(self, fixture, monkeypatch, fail=None, gate=None):
        self.calls, self.fixture, self.fail, self.gate = [], fixture, fail, gate
        real = subprocess.run

        def run(argv, *a, **kw):
            if argv[:2] == ['git', 'clone']:
                self.calls.append({'argv': list(argv), 'env': dict(kw.get('env') or {})})
                if self.gate:
                    self.gate.wait(5)
                if self.fail:
                    return subprocess.CompletedProcess(argv, 128, '', self.fail)
                return real(['git', 'clone', '-q', str(self.fixture), argv[-1]], capture_output=True, text=True)
            return real(argv, *a, **kw)
        monkeypatch.setattr(ms.subprocess, 'run', run)


def _repos(client):
    return {r['project_id']: r for r in client.get('/api/v2/maintenance/repos').json()['repos']}


def _settled(client, pid, deadline=10):
    end = time.time() + deadline
    while time.time() < end:
        view = _repos(client).get(pid)
        if view and view['state'] != 'analyzing':
            return view
        time.sleep(0.05)
    raise AssertionError('clone did not settle')


def _register(client, headers, **extra):
    res = client.post('/api/v2/maintenance/repos', json={'source': URL, 'name': 'group-risk', **extra}, headers=headers)
    return res


def test_without_platform_token_the_failure_says_why_and_what_next(app_env, monkeypatch):
    client, store, svc, repo = app_env
    monkeypatch.delenv('FACTORY_GITHUB_TOKEN', raising=False)
    fake = FakeClone(repo, monkeypatch, fail=NO_AUTH)
    headers = login(client)
    res = _register(client, headers)
    assert res.status_code == 201, res.text
    view = _settled(client, res.json()['project_id'])
    access = view['probe']['access']
    assert view['state'] == 'failed' and access['reason'] == 'auth'
    assert access['message'] == '执行主机没有访问该仓库的凭据'
    assert 'FACTORY_GITHUB_TOKEN' in access['next_step'] and 'terminal prompts disabled' in access['detail']
    assert 'extraheader' not in str(fake.calls[0]['env'])


def test_platform_token_authorises_github_clone_without_leaking_and_retry_keeps_the_project(app_env, monkeypatch):
    client, store, svc, repo = app_env
    monkeypatch.delenv('FACTORY_GITHUB_TOKEN', raising=False)
    fake = FakeClone(repo, monkeypatch, fail=NO_AUTH)
    headers = login(client)
    pid = _register(client, headers).json()['project_id']
    assert _settled(client, pid)['state'] == 'failed'

    # The platform credential becomes available; "重新接入" (probe) clones again.
    monkeypatch.setenv('FACTORY_GITHUB_TOKEN', TOKEN)
    fake.fail = None
    res = client.post(f'/api/v2/maintenance/repos/{pid}/probe', headers=headers)
    assert res.status_code == 200, res.text
    view = _settled(client, pid)
    assert view['state'] in ('needs_input', 'ready'), view
    assert view['project_id'] == pid and len(store.projects()) == 1
    call = fake.calls[-1]
    header = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()
    assert TOKEN not in ' '.join(call['argv']) and header not in ' '.join(call['argv'])
    assert call['argv'][-2] == URL  # plain URL, no userinfo
    assert call['env']['GIT_CONFIG_KEY_0'] == 'http.https://github.com/.extraheader'
    assert call['env']['GIT_CONFIG_VALUE_0'] == f'AUTHORIZATION: basic {header}'
    assert call['env']['GIT_CONFIG_GLOBAL'] and call['env']['GIT_TERMINAL_PROMPT'] == '0'
    workspace = store.project(pid)['workspace']
    config = subprocess.run(['git', 'config', '--list', '--show-origin'], cwd=workspace,
                            capture_output=True, text=True).stdout
    assert TOKEN not in config and header not in config and 'extraheader' not in config
    assert TOKEN not in str(_repos(client))


def test_reregistering_a_failed_url_reclones_into_the_same_project(app_env, monkeypatch):
    client, store, svc, repo = app_env
    monkeypatch.delenv('FACTORY_GITHUB_TOKEN', raising=False)
    fake = FakeClone(repo, monkeypatch, fail=NO_AUTH)
    headers = login(client)
    pid = _register(client, headers).json()['project_id']
    assert _settled(client, pid)['state'] == 'failed'
    monkeypatch.setenv('FACTORY_GITHUB_TOKEN', TOKEN)
    fake.fail = None
    again = _register(client, headers, credential_ref='github')
    assert again.status_code == 201 and again.json()['reused'] is True and again.json()['project_id'] == pid
    assert _settled(client, pid)['state'] in ('needs_input', 'ready')
    assert len(fake.calls) == 2 and len(store.projects()) == 1


def test_unknown_or_misplaced_credential_refs_are_refused_not_ignored(app_env, monkeypatch):
    client, store, svc, repo = app_env
    fake = FakeClone(repo, monkeypatch)
    headers = login(client)
    res = _register(client, headers, credential_ref='corp-vault-key')
    assert res.status_code == 422 and '未在平台配置' in res.text
    res = client.post('/api/v2/maintenance/repos', json={'source': 'https://gitlab.example.com/a/b.git',
                      'name': 'x', 'credential_ref': 'github'}, headers=headers)
    assert res.status_code == 422 and 'github.com' in res.text
    assert not fake.calls and not store.projects()


def test_a_stored_unknown_ref_fails_the_retry_without_cloning(app_env, monkeypatch):
    client, store, svc, repo = app_env
    monkeypatch.setenv('FACTORY_GITHUB_TOKEN', TOKEN)
    fake = FakeClone(repo, monkeypatch, fail=NO_AUTH)
    headers = login(client)
    pid = _register(client, headers).json()['project_id']
    _settled(client, pid)
    with store.connect() as db:  # a record saved before refs were validated
        row = db.execute('SELECT data FROM maintenance_repo_probes WHERE project_id=?', (pid,)).fetchone()
        import json
        data = {**json.loads(row[0]), 'credential_ref': 'legacy-secret'}
        db.execute('UPDATE maintenance_repo_probes SET data=? WHERE project_id=?', (json.dumps(data), pid))
    calls = len(fake.calls)
    client.post(f'/api/v2/maintenance/repos/{pid}/probe', headers=headers)
    view = _settled(client, pid)
    assert view['state'] == 'failed' and view['probe']['access']['reason'] == 'credential_unknown'
    assert len(fake.calls) == calls


def test_a_non_empty_target_is_never_overwritten(app_env, monkeypatch):
    client, store, svc, repo = app_env
    monkeypatch.setenv('FACTORY_GITHUB_TOKEN', TOKEN)
    fake = FakeClone(repo, monkeypatch, fail=NO_AUTH)
    headers = login(client)
    pid = _register(client, headers).json()['project_id']
    _settled(client, pid)
    workspace = store.project(pid)['workspace']
    import pathlib
    keep = pathlib.Path(workspace)
    keep.mkdir(parents=True, exist_ok=True)
    (keep / 'someone-else.txt').write_text('do not touch')
    fake.fail = None
    calls = len(fake.calls)
    client.post(f'/api/v2/maintenance/repos/{pid}/probe', headers=headers)
    view = _settled(client, pid)
    assert view['state'] == 'failed' and view['probe']['access']['reason'] == 'target_occupied'
    assert (keep / 'someone-else.txt').read_text() == 'do not touch' and len(fake.calls) == calls
    assert not [p for p in keep.parent.iterdir() if p.name.startswith('.clone-')]


def test_concurrent_retries_run_one_clone(app_env, monkeypatch):
    client, store, svc, repo = app_env
    monkeypatch.setenv('FACTORY_GITHUB_TOKEN', TOKEN)
    fake = FakeClone(repo, monkeypatch, fail=NO_AUTH)
    headers = login(client)
    pid = _register(client, headers).json()['project_id']
    _settled(client, pid)
    gate = threading.Event()
    fake.fail, fake.gate = None, gate
    calls = len(fake.calls)
    for _ in range(3):
        assert client.post(f'/api/v2/maintenance/repos/{pid}/probe', headers=headers).status_code == 200
    assert _repos(client)[pid]['state'] == 'analyzing'
    gate.set()
    assert _settled(client, pid)['state'] in ('needs_input', 'ready')
    assert len(fake.calls) == calls + 1


def test_failure_detail_is_redacted():
    header = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()
    failure = ms._clone_failure(
        f"fatal: unable to access 'https://user:{TOKEN}@github.com/x/y.git/': The requested URL returned error: 403\nAUTHORIZATION: basic {header}",
        credential='github', github=True, hidden=(TOKEN, header))
    assert TOKEN not in failure['detail'] and header not in failure['detail']
    assert failure['reason'] == 'auth' and '平台 GitHub 凭据' in failure['message']
