"""Synthetic regressions for local data privacy and structured audit redaction."""
import json
import os
from pathlib import Path

import pytest

from factory.control.auth import AuthStore
from factory.control.store import Store, scrub
from factory.redact import MASK, redact


@pytest.mark.parametrize('constructor,filename', [(AuthStore, 'users.db'), (Store, 'control.db')])
def test_fresh_databases_are_private_without_relying_on_process_umask(tmp_path, constructor, filename):
    previous = os.umask(0o022)
    try:
        path = tmp_path / 'new' / 'nested' / filename
        constructor(path)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.parent.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('fn', [redact, scrub])
@pytest.mark.parametrize('key', ['password', 'DEPLOY_PASSWORD', 'clientSecret', 'accessToken',
    'AWS_SECRET_ACCESS_KEY', 'ssh_private_key', 'password_hash', 'csrf_token', 'Authorization'])
def test_structured_secret_values_do_not_escape(fn, key):
    value = {'nested': [{key: 'SYNTHETIC-sensitive-value'}], 'input_tokens': 21,
             'cached_input_tokens': 8, 'token_count': 29, 'session_id': 'session-reference'}
    result = fn(value)
    assert 'SYNTHETIC-sensitive-value' not in json.dumps(result)
    assert result['input_tokens'] == 21
    assert result['cached_input_tokens'] == 8
    assert result['token_count'] == 29
    assert result['session_id'] == 'session-reference'


def test_existing_permissions_and_content_are_not_silently_changed(tmp_path):
    path = tmp_path / 'users.db'
    AuthStore(path)
    path.chmod(0o644)
    before = path.read_bytes()
    AuthStore(path)
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == before


def test_private_creator_does_not_change_process_umask(tmp_path):
    from factory.private_files import prepare_private_database
    previous = os.umask(0o027)
    try:
        prepare_private_database(tmp_path / 'private.db')
        observed = os.umask(0o027)
        assert observed == 0o027
    finally:
        os.umask(previous)


def test_service_template_and_proxy_keep_secure_defaults():
    root = Path(__file__).parents[1]
    assert 'UMask=0077' in (root / 'deploy/factory-control.service').read_text()
    caddy = (root / 'deploy/Caddyfile.example').read_text()
    assert '\nhttp://HOST {' not in caddy
    assert '\nfactory.example.com {' in caddy
    assert "--plaintext '" not in caddy


@pytest.mark.parametrize('key', ['apikey', 'APIKEY', 'APIKey', 'api-key', 'api_key',
    'accesstoken', 'ACCESSTOKEN', 'access-token', 'access_token', 'passwd', 'secret',
    'authorization', 'cookie', 'token', 'csrf_token', 'credential', 'private_key', 'webhook'])
def test_legacy_sensitive_key_aliases_remain_redacted(key):
    assert scrub({key: 'SYNTHETIC-secret'})[key] == MASK
    assert redact({key: 'SYNTHETIC-secret'})[key] == MASK


def test_control_database_wal_sidecars_inherit_private_access(tmp_path):
    path = tmp_path / 'control.db'
    previous = os.umask(0o022)
    try:
        store = Store(path)
        with store.connect() as db:
            db.execute("INSERT INTO projects VALUES ('synthetic', '{}')")
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                assert sidecar.is_file()
                assert sidecar.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous)


def test_legacy_audit_persists_no_structured_secret_values(tmp_path):
    from factory.audit.models import OracleClass, SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    path = tmp_path / 'legacy' / 'audit.db'
    store = AuditStore(path)
    attempt = store.open_attempt(task_id='synthetic', spec_ref=['AC-1'],
        oracle_class=OracleClass.A, class_reason='test', harness='fixture',
        harness_version='1', model='synthetic')
    store.record_verdict(attempt, role=SupervisorRole.REGRESSION, verdict=Verdict.PASS,
        claims=[{'DEPLOY_PASSWORD': 'SYNTHETIC-legacy-secret', 'input_tokens': 12}])
    recorded = store.get(attempt).supervisors[0].claims
    assert 'SYNTHETIC-legacy-secret' not in json.dumps(recorded)
    assert recorded[0]['input_tokens'] == 12
    assert path.stat().st_mode & 0o777 == 0o600
