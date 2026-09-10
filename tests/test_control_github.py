import subprocess
from pathlib import Path

import httpx
import pytest

from factory.control.github import GitHubDelivery


HEAD_SHA = 'a' * 40
MERGE_SHA = 'b' * 40


def _observe(payload, *, artifacts=None, error=None):
    seen = []

    def api(request):
        seen.append(request)
        if error:
            raise error
        return httpx.Response(200, json=payload)

    client = httpx.Client(base_url='https://api.github.com',
                          transport=httpx.MockTransport(api), trust_env=False)
    delivery = GitHubDelivery('fake-test-token', client)
    project = {'repository': 'test/repo', 'base_branch': 'main'}
    run = {'artifacts': artifacts or {
        'repository': 'test/repo', 'pr_number': 7,
        'pr_url': 'https://github.com/test/repo/pull/7',
        'branch': 'factory/abc', 'commit': HEAD_SHA,
    }}
    return delivery, project, run, seen


def _pr(**changes):
    payload = {
        'number': 7, 'html_url': 'https://github.com/test/repo/pull/7',
        'state': 'closed', 'merged': True,
        'merged_at': '2026-09-08T12:00:00Z', 'merge_commit_sha': MERGE_SHA,
        'head': {'ref': 'factory/abc', 'sha': HEAD_SHA,
                 'repo': {'full_name': 'test/repo'}},
        'base': {'ref': 'main', 'repo': {'full_name': 'test/repo'}},
    }
    payload.update(changes)
    return payload


def test_publish_verified_head_and_reconcile_existing_pr(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    remote = tmp_path / 'remote.git'
    work.mkdir()
    run = subprocess.run
    run(['git', 'init', '-q', '-b', 'main'], cwd=work, check=True)
    run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    (work / 'README.md').write_text('baseline')
    run(['git', 'add', '.'], cwd=work, check=True)
    run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.com', 'commit', '-qm', 'baseline'], cwd=work, check=True)
    run(['git', 'push', str(remote), 'main'], cwd=work, check=True, capture_output=True)
    run(['git', 'checkout', '-qb', 'factory/abc'], cwd=work, check=True)
    (work / 'result.txt').write_text('verified')
    run(['git', 'add', 'result.txt'], cwd=work, check=True)
    run(['git', '-c', 'user.name=Test', '-c', 'user.email=t@example.com', 'commit', '-qm', 'verified'], cwd=work, check=True)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=work, text=True).strip()
    seen = []
    def local_push(args, **kwargs):
        if args[1] in ('push', 'fetch', 'ls-remote'):
            assert 'https://github.com/test/repo.git' in args
            assert not any(str(arg).startswith('--force') for arg in args)
            args = [str(remote) if arg == 'https://github.com/test/repo.git' else arg for arg in args]
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
    assert first['pr_number'] == 7
    assert first['repository'] == 'test/repo'
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


def test_observe_merge_returns_verified_evidence():
    delivery, project, run, seen = _observe(_pr())
    assert delivery.observe_merge(project, run) == {
        'merged': True, 'repository': 'test/repo', 'pr_number': 7,
        'pr_url': 'https://github.com/test/repo/pull/7',
        'head_sha': HEAD_SHA, 'merge_commit_sha': MERGE_SHA,
        'base_branch': 'main', 'merged_at': '2026-09-08T12:00:00Z',
    }
    assert seen[0].method == 'GET'
    assert seen[0].url.path == '/repos/test/repo/pulls/7'
    delivery.close()


@pytest.mark.parametrize('state', ['open', 'closed'])
def test_observe_merge_treats_reopened_or_closed_unmerged_as_normal(state):
    delivery, project, run, _ = _observe(_pr(state=state, merged=False,
                                              merged_at=None,
                                              merge_commit_sha=None))
    assert delivery.observe_merge(project, run) == {
        'merged': False, 'reason': 'not_merged', 'pr_number': 7,
        'pr_url': 'https://github.com/test/repo/pull/7',
    }
    delivery.close()


@pytest.mark.parametrize('field,value', [
    ('head', {'ref': 'factory/abc', 'sha': HEAD_SHA,
              'repo': {'full_name': 'other/repo'}}),
    ('base', {'ref': 'main', 'repo': {'full_name': 'other/repo'}}),
    ('base', {'ref': 'other', 'repo': {'full_name': 'test/repo'}}),
    ('head', {'ref': 'other', 'sha': HEAD_SHA,
              'repo': {'full_name': 'test/repo'}}),
])
def test_observe_merge_rejects_wrong_scope(field, value):
    payload = _pr(**{field: value})
    delivery, project, run, seen = _observe(payload)
    with pytest.raises(ValueError):
        delivery.observe_merge(project, run)
    assert len(seen) == 1
    delivery.close()


def test_observe_merge_checks_changed_head_even_when_unmerged():
    delivery, project, run, _ = _observe(_pr(merged=False, merged_at=None,
                                              merge_commit_sha=None,
                                              head={
                                                  'ref': 'factory/abc',
                                                  'sha': 'c' * 40,
                                                  'repo': {'full_name': 'test/repo'},
                                              }))
    with pytest.raises(ValueError):
        delivery.observe_merge(project, run)
    delivery.close()


@pytest.mark.parametrize('url', [
    'https://evil.example/test/repo/pull/7',
    'javascript:alert(1)',
    'https://github.com/test/repo/pull/7?next=https://evil.example',
    'https://github.com/test/repo/pull/7/',
])
def test_observe_merge_rejects_url_injection(url):
    artifacts = {'repository': 'test/repo', 'pr_number': 7, 'pr_url': url,
                 'branch': 'factory/abc', 'commit': HEAD_SHA}
    delivery, project, run, seen = _observe(_pr(), artifacts=artifacts)
    with pytest.raises(ValueError):
        delivery.observe_merge(project, run)
    assert not seen
    delivery.close()


def test_observe_merge_rejects_malformed_head_sha_even_unmerged():
    delivery, project, run, _ = _observe(_pr(merged=False, merged_at=None,
                                              merge_commit_sha=None,
                                              head={
                                                  'ref': 'factory/abc',
                                                  'sha': 'not-a-sha',
                                                  'repo': {'full_name': 'test/repo'},
                                              }))
    with pytest.raises(ValueError):
        delivery.observe_merge(project, run)
    delivery.close()


def test_observe_merge_propagates_network_errors():
    error = httpx.ConnectError('offline')
    delivery, project, run, _ = _observe(_pr(), error=error)
    with pytest.raises(httpx.ConnectError):
        delivery.observe_merge(project, run)
    delivery.close()
