import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.control.auth import AuthStore
from factory.control.store import Store
from factory.control.readiness import inspect_deployment
from factory.control import runtime_cli


@pytest.fixture
def installation(tmp_path):
    data = tmp_path / 'data'
    AuthStore(data / 'users.db')
    Store(data / 'control.db')
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'app.js').write_text('void 0')
    (static / 'index.html').write_text('<script src="/app.js"></script>')
    return dict(data_dir=data, workspace=tmp_path, static_dir=static,
                public_origin='https://factory.example.com',
                isolation_probe=lambda **_: SimpleNamespace(available=True, reason=''))


def statuses(report):
    return {item['id']: item['status'] for item in report['checks']}


def test_local_pass_is_not_enterprise_certification(installation):
    report = inspect_deployment(**installation)
    assert report['status'] == 'passed'
    assert all(item['status'] == 'unknown' for item in report['external_acceptance'])
    assert 'not proof of enterprise readiness' in report['note']


@pytest.mark.parametrize('origin', ['http://factory.example.com', 'https://user:secret@example.com',
    'https://example.com/path', 'https://example.com?secret=value', 'https://example.com:bad'])
def test_insecure_or_invalid_origin_is_blocked_without_echoing_secrets(installation, origin):
    report = inspect_deployment(**{**installation, 'public_origin': origin})
    assert statuses(report)['public_origin'] == 'blocked'
    assert origin not in json.dumps(report)


def test_missing_origin_is_unknown(installation):
    report = inspect_deployment(**{**installation, 'public_origin': None})
    assert report['status'] == 'unknown'
    assert statuses(report)['public_origin'] == 'unknown'


def test_permission_failure_does_not_chmod_or_write_existing_database(installation):
    path = installation['data_dir'] / 'users.db'
    path.chmod(0o644)
    before = path.read_bytes()
    report = inspect_deployment(**installation)
    assert statuses(report)['users.db'] == 'blocked'
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == before


def test_missing_data_does_not_create_live_state(installation):
    data = installation['workspace'] / 'not-created'
    report = inspect_deployment(**{**installation, 'data_dir': data})
    assert statuses(report)['data_directory'] == 'blocked'
    assert not data.exists()


def test_empty_database_is_not_accepted(installation):
    (installation['data_dir'] / 'users.db').write_bytes(b'')
    assert statuses(inspect_deployment(**installation))['users.db'] == 'blocked'


def test_real_probe_failure_is_not_replaced_by_binary_presence(installation):
    report = inspect_deployment(**{**installation, 'isolation_probe':
        lambda **_: SimpleNamespace(available=False, reason='token=SYNTHETIC-secret boundary failed')})
    assert statuses(report)['execution_isolation'] == 'blocked'
    assert 'SYNTHETIC-secret' not in json.dumps(report)


def test_probe_exception_is_unknown_not_passed(installation):
    def unavailable(**_):
        raise OSError('secret=SYNTHETIC-secret')
    report = inspect_deployment(**{**installation, 'isolation_probe': unavailable})
    assert statuses(report)['execution_isolation'] == 'unknown'
    assert 'SYNTHETIC-secret' not in json.dumps(report)


def test_missing_asset_is_a_frontend_blocker(installation):
    (installation['static_dir'] / 'app.js').unlink()
    assert statuses(inspect_deployment(**installation))['frontend'] == 'blocked'


def test_external_asset_is_not_claimed_verified(installation):
    (installation['static_dir'] / 'index.html').write_text('<script src="https://cdn.example.com/app.js"></script>')
    assert statuses(inspect_deployment(**installation))['frontend'] == 'unknown'


@pytest.mark.parametrize('status,exit_code', [('passed', 0), ('blocked', 1), ('unknown', 2)])
def test_cli_exit_codes_keep_unknown_distinct(monkeypatch, capsys, status, exit_code):
    monkeypatch.setattr('factory.control.readiness.inspect_deployment', lambda **_: {
        'status': status, 'checks': [], 'note': 'local only'})
    assert runtime_cli.main(['preflight', '--json']) == exit_code
    assert json.loads(capsys.readouterr().out)['status'] == status


def test_preflight_resolves_service_defaults_independently_of_cwd(tmp_path, monkeypatch, capsys):
    for key in ('FACTORY_CONTROL_DATA', 'FACTORY_WORKSPACE_ROOT', 'FACTORY_STATIC_DIR'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    captured = []
    def inspect(**kwargs):
        captured.append(kwargs)
        return {'status': 'unknown', 'checks': [], 'note': 'fixture'}
    monkeypatch.setattr('factory.control.readiness.inspect_deployment', inspect)
    assert runtime_cli.main(['preflight', '--json']) == 2
    assert captured[0]['data_dir'] == tmp_path / '.factory/control'
    assert captured[0]['workspace'] == tmp_path / 'projects'
    assert captured[0]['static_dir'] == Path(__file__).resolve().parents[1] / 'frontend/dist'


def test_preflight_respects_actual_service_environment_paths(tmp_path, monkeypatch, capsys):
    from factory.control.deployment_paths import data_directory, workspace_directory, static_directory
    monkeypatch.setenv('FACTORY_CONTROL_DATA', str(tmp_path / 'configured-data'))
    monkeypatch.setenv('FACTORY_WORKSPACE_ROOT', str(tmp_path / 'configured-workspaces'))
    monkeypatch.setenv('FACTORY_STATIC_DIR', str(tmp_path / 'configured-static'))
    captured = []
    monkeypatch.setattr('factory.control.readiness.inspect_deployment', lambda **kw:
        captured.append(kw) or {'status': 'unknown', 'checks': [], 'note': 'fixture'})
    runtime_cli.main(['preflight', '--json'])
    assert captured[0]['data_dir'] == data_directory()
    assert captured[0]['workspace'] == workspace_directory()
    assert captured[0]['static_dir'] == static_directory()


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_preflight_checks_existing_sidecar_permissions_without_mutation(installation, suffix):
    sidecar = installation['data_dir'] / ('users.db' + suffix)
    sidecar.write_bytes(b'synthetic-sidecar')
    sidecar.chmod(0o644)
    report = inspect_deployment(**installation)
    assert statuses(report)['users.db' + suffix] == 'blocked'
    assert sidecar.read_bytes() == b'synthetic-sidecar'
    assert sidecar.stat().st_mode & 0o777 == 0o644
