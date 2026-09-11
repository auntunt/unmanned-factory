"""Repository binding, durable creation receipt and admin publication endpoints."""
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_control_app import login, project
from tests.test_workbench_app import app_env


class Publisher:
    def __init__(self):
        self.creates, self.lookups, self.uploads = 0, 0, 0
        self.fail_create = self.fail_upload = False

    def account(self):
        return {'login': 'owner', 'avatar_url': '', 'html_url': 'https://github.com/owner'}

    def repository(self, repository):
        self.lookups += 1
        return {'id': 7, 'full_name': repository, 'name': repository.split('/')[-1],
            'private': True, 'default_branch': 'main', 'html_url': 'https://github.com/' + repository,
            'permissions': {'push': True}}

    def repositories(self, *, page=1):
        return {'repositories': [self.repository('owner/result')], 'page': page, 'has_more': False}

    def create_repository(self, *, name, private):
        assert private is True
        self.creates += 1
        if self.fail_create:
            raise RuntimeError('uncertain creation')
        return self.repository('owner/' + name)

    def publish(self, project, run):
        self.uploads += 1
        if self.fail_upload:
            raise RuntimeError('network failure')
        return {'repository': project['repository'], 'repository_url': 'https://github.com/' + project['repository'],
            'publication_type': 'initial', 'published_branch': 'main', 'commit': run['artifacts']['commit']}


def setup(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    with store.connect() as db:
        original = json.loads(db.execute('SELECT data FROM projects WHERE id=?', (p['id'],)).fetchone()[0])
        original['repository'] = 'local/' + p['id']
        db.execute('UPDATE projects SET data=? WHERE id=?', (json.dumps(original), p['id']))
    p = store.project(p['id'])
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    run, _ = store.create_run(p['id'], 'Build tool', source={'type': 'web', 'actor_id': 1})
    store.update(run['id'], {'status': 'ready_for_review', 'artifacts': {
        'commit': commit, 'base_sha': commit, 'branch': 'main', 'worktree': str(repo)}})
    svc.publisher = Publisher()
    return client, store, svc, headers, p, run['id']


def test_existing_repository_bind_publish_and_catalog(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    options = client.get(f'/api/v3/runs/{rid}/github-options').json()
    assert options['github_configured'] and not options['github_repository_bound']
    assert options['project_revision'] == p['revision']
    response = client.post(f'/api/v3/runs/{rid}/github-publish', headers=headers,
        json={'mode': 'existing', 'repository': 'owner/result', 'expected_project_revision': p['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'published'
    assert response.json()['artifacts']['publication_type'] == 'initial'
    bound = store.project(p['id'])
    assert bound['repository'] == 'owner/result'
    assert bound['base_branch'] == p['base_branch']
    assert bound['github_base_branch'] == 'main'
    assert bound['revision'] == p['revision'] + 1
    assert store.project_audit(p['id'])[-1]['action'] == 'github.bound'
    catalog = client.get(f'/api/v3/runs/{rid}/deliverables').json()
    assert catalog['github_repository_bound']
    assert catalog['repository_url'] == 'https://github.com/owner/result'


def test_historical_verified_run_does_not_block_current_first_publication(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    historical, _ = store.create_run(p['id'], 'Older verified delivery')
    store.update(historical['id'], {'status': 'ready_for_review'})

    result = svc.publish_github(rid, mode='existing', repository='owner/result',
        expected_project_revision=p['revision'], actor='owner')

    assert result['status'] == 'published'
    # Publishing is safe, while advancing the local baseline remains explicit
    # because another verified branch is still waiting for delivery.
    assert result['artifacts']['baseline_sync']['status'] == 'needs_sync'
    assert store.get(historical['id'])['status'] == 'ready_for_review'
    assert store.project(p['id'])['repository'] == 'owner/result'


def test_historical_verified_run_allows_same_repository_but_blocks_rebinding(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    with store.connect() as db:
        bound = json.loads(db.execute('SELECT data FROM projects WHERE id=?', (p['id'],)).fetchone()[0])
        bound.update(repository='owner/result', github_repository_id=7, github_base_branch='main')
        db.execute('UPDATE projects SET data=? WHERE id=?', (json.dumps(bound), p['id']))
    historical, _ = store.create_run(p['id'], 'Older verified delivery')
    store.update(historical['id'], {'status': 'ready_for_review'})

    result = svc.publish_github(rid, mode='bound', expected_project_revision=p['revision'], actor='owner')
    assert result['status'] == 'published'
    assert svc.publisher.lookups == 1

    next_run, _ = store.create_run(p['id'], 'Another verified delivery')
    store.update(next_run['id'], {'status': 'ready_for_review', 'artifacts': store.get(rid)['artifacts']})
    from factory.control.store import Conflict
    with pytest.raises(Conflict, match='待交付'):
        svc.publish_github(next_run['id'], mode='existing', repository='owner/different',
            expected_project_revision=store.project(p['id'])['revision'], actor='owner')
    assert svc.publisher.lookups == 1


def test_create_is_idempotent_even_with_stale_revision_after_success(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    request = dict(mode='create', name='result', private=True, expected_project_revision=p['revision'], actor='owner')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: svc.publish_github(rid, **request), range(2)))
    assert all(result['status'] == 'published' for result in results)
    assert svc.publisher.creates == 1 and svc.publisher.uploads == 1


def test_uncertain_create_never_reposts_and_explicit_existing_recovers(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    request = dict(mode='create', name='result', private=True, expected_project_revision=p['revision'], actor='owner')
    svc.publisher.fail_create = True
    with pytest.raises(RuntimeError):
        svc.publish_github(rid, **request)
    from factory.control.store import Conflict
    with pytest.raises(Conflict, match='尚未确认'):
        svc.publish_github(rid, **request)
    assert svc.publisher.creates == 1
    assert store.get(rid)['status'] == 'ready_for_review'
    result = svc.publish_github(rid, mode='existing', repository='owner/result',
        expected_project_revision=p['revision'], actor='owner')
    assert result['status'] == 'published'
    assert svc.publisher.creates == 1


def test_stale_revision_and_active_project_do_not_create_remote(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    from factory.control.store import Conflict
    with pytest.raises(Conflict, match='已更新'):
        svc.publish_github(rid, mode='create', name='result', expected_project_revision=999, actor='owner')
    other, _ = store.create_run(p['id'], 'still working')
    with pytest.raises(Conflict, match='进行中'):
        svc.publish_github(rid, mode='create', name='result', expected_project_revision=p['revision'], actor='owner')
    assert svc.publisher.creates == 0
    assert store.project(p['id'])['repository'].startswith('local/')


def test_active_job_still_blocks_after_its_durable_status_has_settled(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    settling, _ = store.create_run(p['id'], 'finishing cancellation cleanup')
    store.update(settling['id'], {'status': 'cancelled'})
    svc.active_jobs[settling['id']] = 'execute'
    try:
        from factory.control.store import Conflict
        with pytest.raises(Conflict, match='收尾'):
            svc.publish_github(rid, mode='existing', repository='owner/result',
                expected_project_revision=p['revision'], actor='owner')
        assert svc.publisher.lookups == svc.publisher.creates == svc.publisher.uploads == 0
        assert store.project(p['id'])['repository'].startswith('local/')
    finally:
        svc.active_jobs.pop(settling['id'], None)


def test_publication_receipt_cannot_change_target_after_failed_upload(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    historical, _ = store.create_run(p['id'], 'Older verified delivery')
    store.update(historical['id'], {'status': 'ready_for_review'})
    svc.publisher.fail_upload = True
    with pytest.raises(RuntimeError, match='network failure'):
        svc.publish_github(rid, mode='existing', repository='owner/result',
            expected_project_revision=p['revision'], actor='owner')

    from factory.control.store import Conflict
    with pytest.raises(Conflict, match='另一仓库'):
        svc.publish_github(rid, mode='existing', repository='owner/different',
            expected_project_revision=store.project(p['id'])['revision'], actor='owner')

    assert svc.publisher.uploads == 1
    assert store.project(p['id'])['repository'] == 'owner/result'


def test_failed_upload_preserves_artifacts_and_retry_uses_bound_receipt(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    svc.publisher.fail_upload = True
    body = {'mode': 'create', 'name': 'result', 'expected_project_revision': p['revision']}
    first = client.post(f'/api/v3/runs/{rid}/github-publish', json=body, headers=headers)
    assert first.status_code == 502
    assert store.get(rid)['status'] == 'ready_for_review'
    assert store.get(rid)['artifacts']['commit']

    # A receipt permits the stale pre-binding revision on retry, but cannot
    # bypass activity which began after the first upload attempt.
    concurrent, _ = store.create_run(p['id'], 'Concurrent work')
    store.update(concurrent['id'], {'status': 'running'})
    assert client.post(f'/api/v3/runs/{rid}/github-publish', json=body,
                       headers=headers).status_code == 409
    assert svc.publisher.uploads == 1
    store.update(concurrent['id'], {'status': 'cancelled'})
    svc.active_jobs[concurrent['id']] = 'execute'
    try:
        assert client.post(f'/api/v3/runs/{rid}/github-publish', json=body,
                           headers=headers).status_code == 409
        assert svc.publisher.uploads == 1
    finally:
        svc.active_jobs.pop(concurrent['id'], None)

    svc.publisher.fail_upload = False
    second = client.post(f'/api/v3/runs/{rid}/github-publish', json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert svc.publisher.creates == 1 and svc.publisher.uploads == 2


def test_public_creation_and_missing_csrf_rejected_and_local_legacy_publish_blocked(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    body = {'mode': 'create', 'name': 'result', 'private': False, 'expected_project_revision': p['revision']}
    assert client.post(f'/api/v3/runs/{rid}/github-publish', json=body, headers=headers).status_code == 422
    body['private'] = True
    assert client.post(f'/api/v3/runs/{rid}/github-publish', json=body, headers={'Origin': 'http://testserver'}).status_code == 403
    legacy = client.post(f'/api/v2/runs/{rid}/publish', headers=headers)
    assert legacy.status_code == 409 and '选择' in legacy.text
    assert svc.publisher.creates == svc.publisher.uploads == 0


def test_member_cannot_read_private_repo_picker_or_publish(app_env):
    client, store, svc, headers, p, rid = setup(app_env)
    client.app.state.auth.create_user('member', 'a-long-test-password', role='member')
    response = client.post('/api/auth/login', json={'username': 'member', 'password': 'a-long-test-password'}, headers={'Origin': 'http://testserver'})
    member = {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}
    assert client.get(f'/api/v3/runs/{rid}/github-options').status_code == 403
    assert client.post(f'/api/v3/runs/{rid}/github-publish', headers=member,
        json={'mode': 'existing', 'repository': 'owner/result', 'expected_project_revision': p['revision']}).status_code == 403


def test_binding_receipt_recovers_crash_after_project_commit(app_env, monkeypatch):
    from factory.control.github_publication import GitHubPublication
    client, store, svc, headers, p, rid = setup(app_env)
    save = GitHubPublication._save
    def crash(self, run_id, receipt):
        if receipt.get('bound_revision'):
            raise RuntimeError('crash after binding commit')
        return save(self, run_id, receipt)
    monkeypatch.setattr(GitHubPublication, '_save', crash)
    args = dict(mode='create', name='result', expected_project_revision=p['revision'], actor='owner')
    with pytest.raises(RuntimeError):
        svc.publish_github(rid, **args)
    assert store.project(p['id'])['repository'] == 'owner/result'
    monkeypatch.setattr(GitHubPublication, '_save', save)
    assert svc.publish_github(rid, **args)['status'] == 'published'
    assert svc.publisher.creates == svc.publisher.uploads == 1


def checked_worktree(store, p, rid, tmp_path):
    from pathlib import Path
    def git(root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    target = tmp_path / 'verified'
    git(p['workspace'], 'worktree', 'add', '-b', 'factory/verified', str(target), 'main')
    (target / 'greeting.txt').write_text('finished output')
    git(target, 'commit', '-qam', 'verified output')
    current = store.get(rid)
    artifacts = {**current['artifacts'], 'worktree': str(target), 'branch': 'factory/verified',
        'commit': git(target, 'rev-parse', 'HEAD')}
    store.update(rid, {'artifacts': artifacts})
    return artifacts, git


def test_initial_publication_fast_forwards_clean_unchanged_source(app_env, tmp_path):
    from pathlib import Path
    client, store, svc, headers, p, rid = setup(app_env)
    artifacts, git = checked_worktree(store, p, rid, tmp_path)
    result = svc.publish_github(rid, mode='existing', repository='owner/result',
        expected_project_revision=p['revision'], actor='owner')
    assert result['artifacts']['baseline_sync']['status'] == 'synced'
    assert git(p['workspace'], 'rev-parse', 'main') == artifacts['commit']
    assert Path(p['workspace'], 'greeting.txt').read_text() == 'finished output'


@pytest.mark.parametrize('reason', ['active', 'dirty', 'different_repository'])
def test_initial_baseline_sync_is_explicit_when_unsafe(app_env, tmp_path, reason):
    from pathlib import Path
    from factory.control.github_publication import GitHubPublication
    client, store, svc, headers, p, rid = setup(app_env)
    artifacts, git = checked_worktree(store, p, rid, tmp_path)
    if reason == 'active':
        svc.active_jobs[rid] = 'execute'
    elif reason == 'dirty':
        Path(p['workspace'], 'local-edit.txt').write_text('keep me')
    else:
        other = tmp_path / 'other-repository'
        git(tmp_path, 'clone', '-q', p['workspace'], str(other))
        artifacts['worktree'] = str(other)
    try:
        result = GitHubPublication(svc).sync_initial_baseline({**store.get(rid),
            'artifacts': {**artifacts, 'publication_type': 'initial'}})
        assert result['status'] == 'needs_sync'
        assert git(p['workspace'], 'rev-parse', 'main') == artifacts['base_sha']
    finally:
        svc.active_jobs.pop(rid, None)
