import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from factory.control.deploy_targets import fingerprint
from factory.control.inspections import inspect_run
from factory.control.store import Conflict
from tests.test_control_app import app_env, login, project


@pytest.fixture
def remote_env(app_env, tmp_path, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    monkeypatch.setenv('FACTORY_DEPLOY_KEY_DIR', str(tmp_path / 'deploy-keys'))
    host_key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    config = tmp_path / 'fake-ssh.json'
    config.write_text(json.dumps({'key': host_key, 'output': 'healthy', 'exit_code': 0}))
    calls = tmp_path / 'ssh-calls.jsonl'
    binary = tmp_path / 'bin'; binary.mkdir()
    script = f'''#!{sys.executable}
import json,sys,time,os,subprocess
from pathlib import Path
config=json.loads(Path({str(config)!r}).read_text())
if Path(sys.argv[0]).name == 'ssh-keyscan':
    print('example.test ' + config['key'])
    sys.exit(0)
with open({str(calls)!r}, 'a') as out:
    out.write(json.dumps(sys.argv[1:])+'\\n')
if config.get('hang'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
    Path(config['pidfile']).write_text(str(child.pid))
    time.sleep(90)
print(config['output'])
sys.exit(config['exit_code'])
'''
    for name in ('ssh', 'ssh-keyscan'):
        path = binary / name; path.write_text(script); path.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    values = {'name': '测试服务器', 'host': 'example.test', 'port': 22, 'user': 'deploy',
              'host_fingerprint': fingerprint(host_key), 'commands': {'health_check': 'check-health',
              'service_status': 'status-service', 'fetch_log': '/var/log/service.log', 'deploy': '/srv/deploy', 'rollback': '/srv/rollback'}}
    target = service.targets.create(values, 1)
    service.targets.bind(p['id'], [target['id']], 0, 1)
    return client, store, service, p, headers, target, config, calls


def release(env, **source):
    _, store, service, p, _, target, *_ = env
    run = store.create_run(p['id'], 'release', source={'type': 'web', 'operation': 'release',
          'execute_deploy': True, 'remote_targets': service.targets.snapshot(p['id']), **source})[0]
    return store.update(run['id'], {'status': 'ready_for_review', 'artifacts': {'verification': {'verdict': 'pass'}}})


def test_key_permissions_public_api_no_private_and_revision(remote_env):
    client, store, service, p, headers, target, *_ = remote_env
    key = service.targets.key_path(target['id'])
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    payload = client.get('/api/v2/deploy-targets').text
    assert target['public_key'] in payload and 'PRIVATE KEY' not in payload and str(key) not in payload
    values = {k: target[k] for k in ('name', 'host', 'port', 'user', 'host_fingerprint', 'commands')}
    response = client.put('/api/v2/deploy-targets/' + target['id'], headers=headers, json={**values, 'revision': 1})
    assert response.status_code == 200
    assert client.put('/api/v2/deploy-targets/' + target['id'], headers=headers, json={**values, 'revision': 1}).status_code == 409
    assert client.request('DELETE', '/api/v2/deploy-targets/' + target['id'], headers=headers, json={'revision': 2}).status_code == 409
    service.targets.bind(p['id'], [], 1, 1)
    service.targets.delete(target['id'], 2, 1)
    assert not key.exists()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM deploy_target_audit').fetchone()[0] == 3
        assert 'PRIVATE KEY' not in str([tuple(r) for r in db.execute('SELECT * FROM deploy_target_audit')])


def test_host_pin_rejects_before_ssh_and_enqueues_alarm(remote_env):
    _, store, service, _, _, target, config, calls = remote_env
    data = json.loads(config.read_text()); data['key'] = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode(); config.write_text(json.dumps(data))
    run = release(remote_env)
    result = service.remote.execute(run['id'], target['id'], 'health_check')
    assert result['status'] == 'unverified' and result['error_type'] == 'host_fingerprint'
    assert not calls.exists()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM events WHERE type="remote.executed"').fetchone()[0] == 1
        assert db.execute('SELECT reason FROM operations_outbox WHERE reason IS NOT NULL').fetchone()


def test_fixed_commands_redaction_log_limit_and_strict_ssh(remote_env):
    _, store, service, _, _, target, config, calls = remote_env
    data = json.loads(config.read_text()); data['output'] = '\n'.join(['token=super-secret'] * 700); config.write_text(json.dumps(data))
    run = release(remote_env)
    result = service.remote.execute(run['id'], target['id'], 'fetch_log', lines=500)
    assert result['status'] == 'pass' and len(result['summary'].splitlines()) <= 500
    assert 'super-secret' not in json.dumps(result)
    argv = json.loads(calls.read_text().splitlines()[-1])
    assert argv[-1] == 'tail -n 500 -- /var/log/service.log'
    for flag in ('BatchMode=yes', 'StrictHostKeyChecking=yes', 'IdentityAgent=none', 'PasswordAuthentication=no', 'GlobalKnownHostsFile=/dev/null'):
        assert flag in argv
    with pytest.raises(ValueError):
        service.remote.execute(run['id'], target['id'], 'fetch_log', lines=501)
    with store.connect() as db:
        assert 'super-secret' not in str([tuple(r) for r in db.execute('SELECT payload FROM events WHERE type="remote.executed"')])


def test_timeout_kills_child_group(remote_env, tmp_path):
    _, _, service, _, _, target, config, _ = remote_env
    pidfile = tmp_path / 'child.pid'
    data = json.loads(config.read_text()); data.update(hang=True, pidfile=str(pidfile)); config.write_text(json.dumps(data))
    service.remote.timeout = .4
    run = release(remote_env)
    start = time.monotonic()
    result = service.remote.execute(run['id'], target['id'], 'service_status')
    assert time.monotonic() - start < 2
    assert result['status'] == 'unverified' and result['error_type'] == 'timeout'
    pid = int(pidfile.read_text())
    # A reparented zombie is terminated too; it cannot perform further work.
    import subprocess
    for _ in range(30):
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith('Z'):
            break
        time.sleep(.02)
    assert not state or state.startswith('Z')


@pytest.mark.parametrize('missing', ['release', 'consent', 'verification'])
def test_each_write_precondition_returns_409(remote_env, missing):
    client, store, service, _, headers, target, _, calls = remote_env
    run = release(remote_env, **({'operation': 'startup'} if missing == 'release' else {'execute_deploy': False} if missing == 'consent' else {}))
    if missing == 'verification':
        store.update(run['id'], {'artifacts': {'verification': {'verdict': 'unverified'}}})
    for verb in ('deploy', 'rollback'):
        response = client.post(f"/api/v2/runs/{run['id']}/remote", headers=headers, json={'target_id': target['id'], 'verb': verb})
        assert response.status_code == 409
    assert not calls.exists()


def test_write_once_and_snapshot_binding_guards(remote_env):
    _, _, service, p, _, target, _, calls = remote_env
    run = release(remote_env)
    first = service.remote.execute(run['id'], target['id'], 'deploy')
    assert first['status'] == 'pass'
    assert service.remote.execute(run['id'], target['id'], 'deploy') == first
    assert len(calls.read_text().splitlines()) == 1
    service.targets.bind(p['id'], [], 1, 1)
    with pytest.raises(Conflict):
        service.remote.execute(run['id'], target['id'], 'rollback')


def test_release_consent_fingerprint_and_members_cannot_manage(remote_env, monkeypatch):
    client, _, service, p, headers, target, _, _ = remote_env
    monkeypatch.setattr(service, 'start_plan', lambda rid: None)
    body = {'project_id': p['id'], 'request': 'prepare', 'operation': 'release', 'idempotency_key': 'release-once'}
    first = client.post('/api/v2/runs', headers=headers, json=body)
    assert first.status_code == 201 and first.json()['source']['execute_deploy'] is False
    assert client.post('/api/v2/runs', headers=headers, json={**body, 'execute_deploy': True}).status_code == 409
    allowed = client.post('/api/v2/runs', headers=headers, json={**body, 'execute_deploy': True, 'idempotency_key': 'release-twice'})
    assert allowed.json()['source']['execute_deploy'] is True
    assert target['host'] not in allowed.json()['request']
    client.app.state.auth.create_user('remote-member', 'very-long-password', role='member')
    response = client.post('/api/auth/login', headers={'Origin': 'http://testserver'}, json={'username': 'remote-member', 'password': 'very-long-password'})
    member = {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}
    assert client.get('/api/v2/deploy-targets').status_code == 403
    assert client.post(f"/api/v2/deploy-targets/{target['id']}/test", headers=member).status_code == 403
    assert client.post(f"/api/v2/runs/{allowed.json()['id']}/remote", headers=member, json={'target_id': target['id'], 'verb': 'deploy'}).status_code == 403


def test_read_only_inspection_records_remote_history_without_deploy(remote_env, monkeypatch):
    _, store, service, p, _, target, config, calls = remote_env
    settings = service.inspections.configure(p['id'], enabled=True, interval_s=300, revision=0, actor_id=1, remote_read_only=True)
    rid = service.inspections.tick(settings['next_at'])[0]
    monkeypatch.setattr(service, '_independent_verify', lambda rid, run, p, settings, artifacts: artifacts.update(verification={'verdict': 'pass'}))
    data = json.loads(config.read_text()); data['exit_code'] = 1; config.write_text(json.dumps(data))
    inspect_run(service, rid)
    assert store.get(rid)['status'] == 'inspection_completed'
    service.operations_automation.tick()
    history = service.operations_automation.history(p['id'])['history'][0]
    assert history['verdict'] == 'unverified' and len(history['remote_results']) == 2
    assert [json.loads(line)[-1] for line in calls.read_text().splitlines()] == ['check-health', 'status-service']
    with pytest.raises(Conflict):
        service.remote.execute(rid, target['id'], 'deploy')


def test_connection_test_only_runs_fixed_probe(remote_env):
    _, _, service, _, _, target, _, calls = remote_env
    assert service.remote.test(target['id'])['status'] == 'pass'
    assert json.loads(calls.read_text().splitlines()[-1])[-1] == 'true'


def test_retries_are_bounded_and_write_failures_notify(remote_env, monkeypatch):
    _, store, service, _, _, target, config, calls = remote_env
    data = json.loads(config.read_text()); data['exit_code'] = 255; config.write_text(json.dumps(data))
    run = release(remote_env)
    assert service.remote.execute(run['id'], target['id'], 'service_status')['attempts'] == 2
    assert len(calls.read_text().splitlines()) == 2
    result = service.remote.execute(run['id'], target['id'], 'deploy')
    assert result['status'] == 'unverified' and result['attempts'] == 1
    service.operations_automation.configure(0, False, 'https://open.feishu.cn/open-apis/bot/v2/hook/test-hook')
    sent = []
    monkeypatch.setattr(service.operations_automation, 'send', lambda url, text: sent.append(text))
    service.operations_automation.tick()
    assert len(sent) == 1 and 'deploy' in sent[0]
    service.operations_automation.tick()
    assert len(sent) == 1


def test_concurrent_write_claim_and_config_changes(remote_env):
    from concurrent.futures import ThreadPoolExecutor
    _, _, service, _, _, target, _, calls = remote_env
    run = release(remote_env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.remote.execute(run['id'], target['id'], 'deploy'), range(2)))
    assert all(r['status'] == 'pass' for r in results)
    assert len(calls.read_text().splitlines()) == 1
    values = {k: target[k] for k in ('name', 'host', 'port', 'user', 'host_fingerprint', 'commands')}
    service.targets.update(target['id'], values, 1, 1)
    with pytest.raises(Conflict):
        service.remote.execute(run['id'], target['id'], 'rollback')


def test_registration_rejects_unknown_fields_without_echoing_keys(remote_env):
    client, _, _, _, headers, target, *_ = remote_env
    values = {k: target[k] for k in ('name', 'host', 'port', 'user', 'host_fingerprint', 'commands')}
    response = client.post('/api/v2/deploy-targets', headers=headers, json={**values, 'private_key': 'PRIVATE-SECRET'})
    assert response.status_code == 422 and 'PRIVATE-SECRET' not in response.text
    assert client.post('/api/v2/deploy-targets', headers=headers, json={**values, 'commands': {'shell': 'anything'}}).status_code == 422
    assert client.post('/api/v2/deploy-targets', headers=headers, json={**values, 'host': '-oProxyCommand=bad'}).status_code == 422


def test_structured_requests_cannot_supply_commands_or_escalate(remote_env):
    _, _, service, _, _, target, _, calls = remote_env
    run = release(remote_env, execute_deploy=False)
    artifacts = {'verification': {'verdict': 'pass', 'remote_requests': [
        {'target_id': target['id'], 'verb': 'rollback'},
        {'target_id': target['id'], 'verb': 'fetch_log', 'command': 'whoami'},
        {'target_id': target['id'], 'verb': 'fetch_log', 'lines': 3},
    ]}}
    service.remote.collect(run['id'], artifacts)
    commands = [json.loads(line)[-1] for line in calls.read_text().splitlines()]
    assert commands == ['check-health', 'status-service', 'tail -n 3 -- /var/log/service.log']


def test_keys_are_hidden_by_all_sandboxes(remote_env, tmp_path):
    from factory.control.claude_terminal import command_argv
    from factory.harness.sandbox_linux import build_argv
    from factory.harness.sandbox_macos import policy_for
    _, _, service, p, _, target, *_ = remote_env
    private = str(service.targets.key_path(target['id']).parent)
    workspace = Path(p['workspace'])
    scratch = tmp_path / 'scratch'; scratch.mkdir()
    assert ['--tmpfs', private] in [command_argv(workspace, scratch, 'true')[i:i+2] for i in range(len(command_argv(workspace, scratch, 'true')))]
    argv = build_argv(['true'], workspace, scratch)
    assert any(argv[i:i+2] == ['--tmpfs', private] for i in range(len(argv)))
    policy = policy_for(workspace, tmp_dir=scratch)
    profile, params = policy.profile()
    assert 'deny file-read* file-write*' in profile and 'DEPLOY_KEYS=' + private in params
    with pytest.raises(ValueError):
        command_argv(Path(private), scratch, 'true')


def test_release_workflow_verifies_before_automatic_deploy(remote_env, monkeypatch):
    from factory.control.autonomy import DEFAULT_POLICY
    from tests.test_control_app import wait_state
    client, store, service, p, headers, target, _, calls = remote_env
    service.policies.update(p['id'], {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'test')
    verified = []
    def verify(rid, run, project, settings, artifacts):
        assert not calls.exists()
        verified.append(rid)
        artifacts['verification'] = {'verdict': 'pass'}
    monkeypatch.setattr(service, '_independent_verify', verify)
    response = client.post('/api/v2/runs', headers=headers, json={'project_id': p['id'], 'request': 'Update greeting', 'operation': 'release', 'execute_deploy': True})
    rid = response.json()['id']
    run = wait_state(store, rid, {'awaiting_approval', 'needs_human'})
    assert client.post(f'/api/v2/runs/{rid}/approve', headers=headers, json={'revision': run['revision']}).status_code == 200
    wait_state(store, rid, {'ready_for_review', 'needs_human'})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and 'remote_results' not in store.get(rid)['artifacts']:
        time.sleep(.05)
    final = store.get(rid)
    assert verified == [rid]
    assert final['status'] == 'ready_for_review'
    assert [r['verb'] for r in final['artifacts']['remote_results']] == ['deploy', 'health_check', 'service_status']
    assert all(r['status'] == 'pass' for r in final['artifacts']['remote_results'])


def test_pending_write_receipt_survives_crash_without_replay(remote_env):
    client, store, service, _, _, target, _, calls = remote_env
    run = release(remote_env)
    pending = {'target_id': target['id'], 'target': target['name'], 'verb': 'deploy', 'status': 'unverified', 'exit_code': None,
               'executed': False, 'duration_s': 0, 'reason': 'unknown receipt'}
    with store.connect() as db:
        db.execute('INSERT INTO remote_invocations VALUES(?,?,?,?)', (run['id'], target['id'], 'deploy', json.dumps(pending)))
    response = client.get('/api/v2/runs/' + run['id'])
    assert response.json()['artifacts']['remote_results'] == [pending]
    assert service.remote.execute(run['id'], target['id'], 'deploy') == pending
    assert not calls.exists()


def test_key_directory_cannot_repurpose_existing_directory(remote_env, tmp_path, monkeypatch):
    _, _, service, _, _, _, *_ = remote_env
    existing = tmp_path / 'unrelated'; existing.mkdir(mode=0o755)
    (existing / 'user-file').write_text('preserve')
    monkeypatch.setenv('FACTORY_DEPLOY_KEY_DIR', str(existing))
    with pytest.raises(Conflict):
        service.targets.key_dir()
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


def test_real_linux_worker_cannot_read_coordinator_key(remote_env, tmp_path):
    import shlex
    import subprocess
    from factory.control.claude_terminal import available, command_argv
    if not available():
        pytest.skip('Linux bubblewrap required')
    _, _, service, p, _, target, *_ = remote_env
    key = service.targets.key_path(target['id'])
    scratch = tmp_path / 'real-scratch'; scratch.mkdir()
    result = subprocess.run(command_argv(Path(p['workspace']), scratch,
        'if test -r ' + shlex.quote(str(key)) + '; then exit 42; else printf BLOCKED; fi'),
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'BLOCKED'


def test_inspection_alert_waits_for_terminal_history_and_deduplicates_pin_failure(remote_env, monkeypatch):
    _, store, service, p, _, _, config, _ = remote_env
    data = json.loads(config.read_text()); data['key'] = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode(); config.write_text(json.dumps(data))
    service.operations_automation.configure(0, False, 'https://open.feishu.cn/open-apis/bot/v2/hook/test-hook')
    sent = []
    monkeypatch.setattr(service.operations_automation, 'send', lambda url, text: sent.append(text))
    settings = service.inspections.configure(p['id'], enabled=True, interval_s=300, revision=0, actor_id=1, remote_read_only=True)
    rid = service.inspections.tick(settings['next_at'])[0]
    monkeypatch.setattr(service, '_independent_verify', lambda rid, run, p, settings, artifacts: artifacts.update(verification={'verdict': 'pass'}))
    record = service.remote._record
    def record_and_tick(run, result):
        record(run, result)
        service.operations_automation.tick()
        assert service.operations_automation.history(p['id'])['history'] == []
        assert sent == []
    monkeypatch.setattr(service.remote, '_record', record_and_tick)
    inspect_run(service, rid)
    service.operations_automation.tick()
    history = service.operations_automation.history(p['id'])['history']
    assert len(history) == 1 and history[0]['verdict'] == 'unverified'
    assert len(history[0]['remote_results']) == 2
    assert len(sent) == 1


@pytest.mark.parametrize('standalone', [False, True])
@pytest.mark.parametrize('method', ['GET', 'PUT', 'DELETE'])
def test_missing_target_returns_404(remote_env, method, standalone):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from factory.control.remote_routes import router

    client, _, service, _, headers, target, *_ = remote_env
    if standalone:
        app = FastAPI()

        @app.middleware('http')
        async def admin_identity(request, call_next):
            request.state.user = {'id': 1, 'role': 'admin'}
            return await call_next(request)

        app.include_router(router(service))
        client = TestClient(app)
    values = {k: target[k] for k in ('name', 'host', 'port', 'user', 'host_fingerprint', 'commands')}
    kwargs = {} if method == 'GET' else {'json': {'revision': 1}}
    if method == 'PUT':
        kwargs['json'].update(values)
    response = client.request(method, '/api/v2/deploy-targets/' + '0' * 32, headers=headers, **kwargs)
    assert response.status_code == 404
    assert response.json() == {'detail': '记录不存在'}
