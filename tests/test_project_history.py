import subprocess

import pytest

from factory.control import history
from factory.control.knowledge import KnowledgeStore
from factory.control.store import Store


def _repo(tmp_path, name='repo'):
    root = tmp_path / name
    root.mkdir()
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'Test'],
                 ['config', 'user.email', 'test@example.com']):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'README.md').write_text('base\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=root, check=True)
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    return root, base


def _fixture(tmp_path):
    root, merge_sha = _repo(tmp_path)
    store = Store(tmp_path / 'control.db')
    project = store.add_project({'name': 'History', 'repository': 'owner/repo',
                                 'workspace': str(root), 'base_branch': 'main', 'checks': {}})
    knowledge = KnowledgeStore(store)
    run = {'id': 'run-1', 'project_id': project['id'], 'plan': {'tasks': []}}
    evidence = {
        'merged': True, 'repository': 'owner/repo', 'pr_number': 7,
        'pr_url': 'https://github.com/owner/repo/pull/7', 'head_sha': 'a' * 40,
        'merge_commit_sha': merge_sha, 'base_branch': 'main',
        'merged_at': '2026-09-08T12:00:00Z', 'check_results': [],
    }
    entry = knowledge.record_merge(project['id'], run, evidence)['entry']
    return root, store, project, knowledge, entry, merge_sha


def _descendant(root):
    (root / 'README.md').write_text('descendant\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'descendant'], cwd=root, check=True)
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def test_descendant_baseline_is_applicable_and_cached(tmp_path, monkeypatch):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    baseline = _descendant(root)
    calls = []
    original = history.subprocess.run

    def counted(args, **kwargs):
        calls.append((args, kwargs))
        return original(args, **kwargs)

    monkeypatch.setattr(history.subprocess, 'run', counted)
    cache = {}
    assert history.historical_merge_applicable(store, project, entry, baseline, cache)
    assert history.historical_merge_applicable(store, project, entry, baseline, cache)
    assert len(calls) == 1
    assert calls[0][0] == ['git', 'merge-base', '--is-ancestor', merge_sha, baseline]
    assert calls[0][1]['cwd'] == str(root)
    assert calls[0][1]['timeout'] == 5


@pytest.mark.parametrize('baseline', ['f' * 40, None, 'not-a-sha'])
def test_unrelated_missing_or_invalid_baseline_fails_closed(tmp_path, baseline):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    assert not history.historical_merge_applicable(store, project, entry, baseline, {})


def test_manually_edited_and_retired_versions_are_not_historical_facts(tmp_path):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    baseline = _descendant(root)
    edited = knowledge.put_entry(project['id'], {
        'kind': 'fact', 'status': 'active', 'title': entry['title'],
        'content': 'human changed this', 'paths': entry['paths'],
        'commit_sha': merge_sha,
    }, 'reviewer', key=entry['key'], expected_revision=1)
    assert not history.historical_merge_applicable(store, project, edited, baseline, {})
    retired = knowledge.put_entry(project['id'], {
        'kind': 'fact', 'status': 'retired', 'title': edited['title'],
        'content': edited['content'], 'paths': edited['paths'],
        'commit_sha': merge_sha,
    }, 'reviewer', key=entry['key'], expected_revision=2)
    assert not history.historical_merge_applicable(store, project, retired, baseline, {})


def test_unknown_source_and_cross_project_key_fail_closed(tmp_path):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    human = knowledge.put_entry(project['id'], {
        'kind': 'fact', 'status': 'active', 'title': 'Human fact',
        'content': 'not a merge', 'paths': [], 'commit_sha': merge_sha,
    }, 'reviewer')
    assert not history.historical_merge_applicable(store, project, human, merge_sha, {})
    other = {**project, 'id': 'not-this-project'}
    assert not history.historical_merge_applicable(store, other, entry, merge_sha, {})


def test_cache_is_bounded_to_32_unique_subprocess_checks(tmp_path, monkeypatch):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    calls = []

    class Green:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Green()

    monkeypatch.setattr(history.subprocess, 'run', fake_run)
    cache = {}
    for index in range(33):
        baseline = f'{index + 1:040x}'
        assert history.historical_merge_applicable(store, project, entry, baseline, cache) is (index < 32)
    assert len(calls) == 32
    assert len(cache) == 32


def test_cached_ancestry_never_bypasses_later_authenticity_check(tmp_path):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    cache = {}
    assert history.historical_merge_applicable(store, project, entry, merge_sha, cache)
    edited = knowledge.put_entry(project['id'], {
        'kind': 'fact', 'status': 'active', 'title': entry['title'],
        'content': 'edited after the cached decision', 'paths': entry['paths'],
        'commit_sha': merge_sha,
    }, 'reviewer', key=entry['key'], expected_revision=1)
    assert not history.historical_merge_applicable(store, project, edited, merge_sha, cache)


def test_invalid_entry_workspace_and_git_errors_fail_closed(tmp_path, monkeypatch):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    baseline = _descendant(root)
    malformed = {**entry, 'commit_sha': 'bad', 'provenance': {'source': 'github_merge'}}
    assert not history.historical_merge_applicable(store, project, malformed, baseline, {})
    assert not history.historical_merge_applicable(store, {**project, 'workspace': str(tmp_path / 'missing')},
                                                   entry, baseline, {})

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(history.subprocess, 'run', fail)
    assert not history.historical_merge_applicable(store, project, entry, baseline, {})


def test_timeout_failures_still_consume_the_32_pair_budget(tmp_path, monkeypatch):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)
    calls = []

    def fail(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(history.subprocess, 'run', fail)
    cache = {}
    for index in range(33):
        baseline = f'{index + 101:040x}'
        assert not history.historical_merge_applicable(store, project, entry, baseline, cache)
    assert len(calls) == 32
    assert len(cache) == 32
    assert all(value is False for value in cache.values())


def test_same_commit_can_be_verified_without_subprocess(tmp_path, monkeypatch):
    root, store, project, knowledge, entry, merge_sha = _fixture(tmp_path)

    def fail(*args, **kwargs):
        raise AssertionError('same commit should use the safe fast path')

    monkeypatch.setattr(history.subprocess, 'run', fail)
    cache = {}
    assert history.historical_merge_applicable(store, project, entry, merge_sha, cache)
    assert cache[(merge_sha, merge_sha)] is True
