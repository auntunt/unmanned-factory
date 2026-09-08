import subprocess
from pathlib import Path

import httpx
import pytest

from factory.control.github import GitHubDelivery


def test_publish_verified_head_and_reconcile_existing_pr(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    remote = tmp_path / 'remote.git'
    work.mkdir()
    run = subprocess.run
    run(['git', 'init', '-q', '-b', 'main'], cwd=work, check=True)
    run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    (work / 'result.txt').write_text('verified')
    run(['git', 'add', 'result.txt'], cwd=work, check=True)
    run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.com', 'commit', '-qm', 'verified'], cwd=work, check=True)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=work, text=True).strip()
    seen = []
    def local_push(args, **kwargs):
        if args[:2] == ['git', 'push']:
            assert args[2] == 'https://github.com/test/repo.git'
            assert '--force' not in args
            args = [*args[:2], str(remote), *args[3:]]
        return run(args, **kwargs)
    monkeypatch.setattr('factory.control.github.subprocess.run', local_push)
    def api(request):
        seen.append(request.method)
        payload = {'html_url': 'https://github.com/test/repo/pull/7', 'head': {'sha': sha}}
        return httpx.Response(200 if request.method == 'GET' else 201,
                              json=([payload] if seen.count('POST') else []) if request.method == 'GET' else payload)
    client = httpx.Client(base_url='https://api.github.com', transport=httpx.MockTransport(api))
    publisher = GitHubDelivery('fake-test-token', client)
    project = {'repository': 'test/repo', 'base_branch': 'main'}
    task = {'id': 'abc', 'revision': 1, 'source': {},
        'plan': {'summary': 'Verified local change', 'title': 'Change'},
        'artifacts': {'branch': 'factory/abc', 'commit': sha, 'worktree': str(work),
                      'checks': [{'name': 'check', 'exit': 0}]}}
    first = publisher.publish(project, task)
    assert first == publisher.publish(project, task)
    assert seen.count('POST') == 1
    assert subprocess.check_output(['git', '--git-dir', str(remote), 'rev-parse', 'factory/abc'], text=True).strip() == sha
    (work / 'result.txt').write_text('changed after verification')
    with pytest.raises(ValueError, match='工作区已变化'):
        publisher.publish(project, task)
    publisher.close()


def test_publish_requires_credentials_and_valid_commit():
    publisher = GitHubDelivery('', httpx.Client(trust_env=False))
    with pytest.raises(ValueError, match='TOKEN'):
        publisher.publish({}, {})
    publisher.close()
