import socket
import subprocess
from pathlib import Path

from factory.control.providers import _claude_tool_allowed
from factory.control.claude_capabilities import effort, web_tool_allowed, researcher_allowed
import pytest


@pytest.mark.parametrize('name', ['.env.example', '.env.local', 'PasswordInput.tsx', 'secret-validation.test.ts', 'credentials-form.ts', 'test.key'])
def test_normal_project_files_are_writable(tmp_path, name):
    assert _claude_tool_allowed('Write', {'file_path': str(tmp_path / name)}, tmp_path, False)
    assert not _claude_tool_allowed('Write', {'file_path': str(tmp_path / name)}, tmp_path, True)


def test_metadata_and_symlink_escape_remain_blocked(tmp_path):
    assert not _claude_tool_allowed('Write', {'file_path': str(tmp_path / '.git/config')}, tmp_path, False)
    outside = tmp_path.parent / 'outside-config'
    (tmp_path / 'link').symlink_to(outside)
    assert not _claude_tool_allowed('Write', {'file_path': str(tmp_path / 'link')}, tmp_path, False)


def test_public_document_tools_reject_private_endpoints(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    assert web_tool_allowed('WebFetch', {'url': 'https://example.com/docs'})
    assert web_tool_allowed('WebSearch', {'query': 'official API documentation'})
    assert not web_tool_allowed('WebFetch', {'url': 'http://example.com'})
    assert not web_tool_allowed('WebFetch', {'url': 'https://user:pass@example.com'})
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    assert not web_tool_allowed('WebFetch', {'url': 'https://example.com'})


def test_research_agent_cannot_override_scope():
    data = {'subagent_type': 'webuddy-research', 'prompt': 'Find the date parser'}
    assert researcher_allowed(data)
    for change in ({'subagent_type': 'general-purpose'}, {'model': 'other'}, {'run_in_background': True}, {'resume': 'other-session'}):
        assert not researcher_allowed({**data, **change})


def test_effort_is_explicit_and_validated(monkeypatch):
    monkeypatch.delenv('WEBUDDY_CLAUDE_EFFORT', raising=False)
    assert effort() == 'medium'
    monkeypatch.setenv('WEBUDDY_CLAUDE_EFFORT', 'high')
    assert effort() == 'high'
    monkeypatch.setenv('WEBUDDY_CLAUDE_EFFORT', 'unknown')
    with pytest.raises(ValueError): effort()


def test_ignored_runtime_files_are_not_staged_but_source_is_inspected(tmp_path):
    from factory.control.execution import _status_paths
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / '.gitignore').write_text('.env.local\n*.log\nhidden.py\n')
    (tmp_path / '.env.local').write_text('DEVELOPMENT_MODE=1')
    (tmp_path / 'preview.log').write_text('server ready')
    (tmp_path / 'hidden.py').write_text('print(1)')
    paths = _status_paths(tmp_path, timeout_s=5)
    assert '.env.local' not in paths and 'preview.log' not in paths
    assert 'hidden.py' in paths
    subprocess.run(['git', 'add', '-f', '.env.local'], cwd=tmp_path, check=True)
    assert '.env.local' in _status_paths(tmp_path, timeout_s=5)


def test_download_includes_templates_but_not_runtime_credentials():
    from factory.control.deliverables import safe_path
    assert safe_path('.env.example')
    assert safe_path('config/.env.template')
    assert not safe_path('.env') and not safe_path('.env.production')
