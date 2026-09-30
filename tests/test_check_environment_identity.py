"""Saved passes belong to the complete sanitized check environment."""
import json
import subprocess
import sys

import pytest

from factory.control import evidence_identity
from factory.harness.checkenv import check_env


def identity(root, argv=None, **kwargs):
    return evidence_identity.check_identity(
        root, 'behavior', argv or [sys.executable, '-c', 'pass'],
        code_signature='unchanged', paths=[], **kwargs)


@pytest.mark.parametrize('key', [
    'CI', 'TZ', 'LANG', 'LC_ALL', 'PYTHONHASHSEED', 'HOME', 'RUSTUP_HOME',
])
def test_forwarded_environment_changes_invalidate_saved_pass(tmp_path, monkeypatch, key):
    monkeypatch.setenv(key, 'first')
    before = identity(tmp_path)
    monkeypatch.setenv(key, 'second')
    after = identity(tmp_path)
    record = {'exit': 0, 'identity_fingerprint': evidence_identity.fingerprint(before)}
    assert evidence_identity.reusable(record, after) == (
        False, 'identity changed since that result')


def test_ci_change_that_breaks_actual_command_invalidates_pass(tmp_path, monkeypatch):
    argv = [sys.executable, '-c', "import os; assert os.environ.get('CI') != '1'"]
    monkeypatch.setenv('CI', '0')
    before = identity(tmp_path, argv)
    assert subprocess.run(argv, cwd=tmp_path, env=check_env(), capture_output=True).returncode == 0
    monkeypatch.setenv('CI', '1')
    after = identity(tmp_path, argv)
    assert subprocess.run(argv, cwd=tmp_path, env=check_env(), capture_output=True).returncode != 0
    assert evidence_identity.fingerprint(before) != evidence_identity.fingerprint(after)


def test_excluded_host_secret_does_not_invalidate_or_leak(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORY_TEST_API_KEY', 'excluded-secret-first')
    before = identity(tmp_path)
    monkeypatch.setenv('FACTORY_TEST_API_KEY', 'excluded-secret-second')
    after = identity(tmp_path)
    assert evidence_identity.fingerprint(before) == evidence_identity.fingerprint(after)
    assert 'FACTORY_TEST_API_KEY' not in check_env()
    assert 'excluded-secret' not in json.dumps(after)


def test_environment_identity_is_order_independent_and_contains_no_raw_values(tmp_path):
    env = {'PATH': '/some/private/path', 'CI': 'private-ci-marker'}
    before = identity(tmp_path, env=env)
    after = identity(tmp_path, env=dict(reversed(list(env.items()))))
    assert evidence_identity.fingerprint(before) == evidence_identity.fingerprint(after)
    assert 'private-ci-marker' not in json.dumps(before)
    assert '/some/private/path' not in json.dumps(before)
    assert len(before['tool']['environment_digest']) == 64


def test_absent_and_empty_forwarded_environment_are_distinct(tmp_path):
    absent = identity(tmp_path, env={})
    empty = identity(tmp_path, env={'CI': ''})
    assert evidence_identity.fingerprint(absent) != evidence_identity.fingerprint(empty)
