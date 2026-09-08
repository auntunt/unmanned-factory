import hashlib
import hmac
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.test_control_app import app_env, login, project, wait_state


HEAD_SHA = 'a' * 40
MERGE_SHA = 'b' * 40


def _entry(title='Greeting fact', content='The greeting is validated.', *, status='active'):
    return {'kind': 'fact', 'status': status, 'title': title, 'content': content,
            'paths': ['greeting.txt'], 'commit_sha': None}


def _signed_webhook(client, payload, *, event='pull_request', delivery='merge-1'):
    raw = json.dumps(payload, separators=(',', ':')).encode()
    signature = 'sha256=' + hmac.new(b'test-webhook-secret', raw, hashlib.sha256).hexdigest()
    return client.post('/api/v2/github/webhook', content=raw, headers={
        'X-GitHub-Event': event, 'X-GitHub-Delivery': delivery,
        'X-Hub-Signature-256': signature,
    })


def _published_run(store, project_data, *, number, status='published'):
    run, _ = store.create_run(project_data['id'], f'published run {number}')
    artifacts = {
        'repository': project_data['repository'], 'pr_number': number,
        'pr_url': f"https://github.com/{project_data['repository']}/pull/{number}",
        'branch': 'factory/abc', 'commit': HEAD_SHA,
    }
    return store.update(run['id'], {
        'status': status, 'artifacts': artifacts,
        'plan': {'tasks': [{'paths': ['greeting.txt']}]},
    }, expected=('received',))


class FakePublisher:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def observe_merge(self, project_data, run):
        number = run['artifacts']['pr_number']
        self.calls.append(number)
        outcome = self.outcomes[number]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _merged_evidence(number=7):
    return {
        'merged': True, 'repository': 'owner/sample', 'pr_number': number,
        'pr_url': f'https://github.com/owner/sample/pull/{number}',
        'head_sha': HEAD_SHA, 'merge_commit_sha': MERGE_SHA,
        'base_branch': 'main', 'merged_at': '2026-09-08T12:00:00Z',
        'check_results': ['greeting=0'],
    }


def test_project_agent_knowledge_crud_cas_scope_and_auth_csrf(app_env):
    client, store, svc, repo = app_env
    assert client.get('/api/v2/projects/no-project/agent').status_code == 401
    headers = login(client)
    p = project(client, repo, headers)

    profile = client.get(f"/api/v2/projects/{p['id']}/agent", headers=headers)
    assert profile.status_code == 200
    assert profile.json()['revision'] == 1
    update = {'expected_revision': 1, 'name': 'Greeting Agent', 'mission': 'Keep greetings correct.',
              'architecture_summary': 'A tiny checked repository.', 'constraints': ['No network']}
    assert client.put(f"/api/v2/projects/{p['id']}/agent", json=update,
                      headers={**headers, 'X-CSRF-Token': 'wrong'}).status_code == 403
    changed = client.put(f"/api/v2/projects/{p['id']}/agent", json=update, headers=headers)
    assert changed.status_code == 200
    assert changed.json()['revision'] == 2
    assert client.put(f"/api/v2/projects/{p['id']}/agent", json=update, headers=headers).status_code == 409

    created = client.post(f"/api/v2/projects/{p['id']}/knowledge", json=_entry(), headers=headers)
    assert created.status_code == 201, created.text
    key = created.json()['key']
    edit = {**_entry(content='The greeting remains hello.'), 'expected_revision': 1}
    edited = client.put(f"/api/v2/projects/{p['id']}/knowledge/{key}", json=edit, headers=headers)
    assert edited.status_code == 200
    assert edited.json()['revision'] == 2
    assert len(client.get(f"/api/v2/projects/{p['id']}/knowledge/{key}/versions",
                          headers=headers).json()['versions']) == 2

    other = client.post('/api/v2/projects', json={
        'name': 'Other', 'repository': 'owner/other', 'workspace': str(repo),
        'checks': {'greeting': ['python', '-c', 'pass']},
    }, headers=headers)
    assert other.status_code == 201, other.text
    other_id = other.json()['id']
    assert client.get(f'/api/v2/projects/{other_id}/knowledge/{key}/versions',
                      headers=headers).status_code == 404
    assert client.get(f'/api/v2/projects/{other_id}/agent', headers=headers).json()['project_id'] == other_id


def test_wiki_import_preview_apply_strict_indices_hash_replay(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    bundle = {'repository': 'owner/sample', 'documents': [
        {'path': 'teamwiki/guide.md', 'content': '# Guide\nUse the greeting check.'},
        {'path': 'teamwiki/decisions.md', 'content': '# Decision\nKeep it simple.'},
    ]}
    preview_response = client.post(f"/api/v2/projects/{p['id']}/wiki-import/preview",
                                   json=bundle, headers=headers)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    endpoint = f"/api/v2/projects/{p['id']}/wiki-import/apply"
    bad_hash = {'preview_id': preview['id'], 'sha256': '0' * 64, 'indices': [0]}
    assert client.post(endpoint, json=bad_hash, headers=headers).status_code == 409
    bad_index = {'preview_id': preview['id'], 'sha256': preview['sha256'], 'indices': [2]}
    assert client.post(endpoint, json=bad_index, headers=headers).status_code == 400
    duplicate_indices = {'preview_id': preview['id'], 'sha256': preview['sha256'], 'indices': [0, 0]}
    assert client.post(endpoint, json=duplicate_indices, headers=headers).status_code in (400, 422)
    applied = {'preview_id': preview['id'], 'sha256': preview['sha256'], 'indices': [0, 1]}
    first = client.post(endpoint, json=applied, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()['duplicate'] is False
    replay = client.post(endpoint, json=applied, headers=headers)
    assert replay.status_code == 200
    assert replay.json()['duplicate'] is True
    assert len(client.get(f"/api/v2/projects/{p['id']}/knowledge", headers=headers).json()['entries']) == 2


def test_frozen_context_ignores_candidates_and_preserves_historical_revisions(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    active = client.post(f"/api/v2/projects/{p['id']}/knowledge", json=_entry(
        title='Greeting architecture', content='Original reviewed guidance.'), headers=headers).json()
    candidate = client.post(f"/api/v2/projects/{p['id']}/knowledge", json=_entry(
        title='Greeting candidate', content='Unreviewed candidate must stay out.', status='candidate'), headers=headers)
    assert candidate.status_code == 201
    indexed = client.post(f"/api/v2/projects/{p['id']}/code-index", headers=headers)
    assert indexed.status_code == 200, indexed.text
    rid = client.post('/api/v2/runs', json={'project_id': p['id'],
                                            'request': 'Update greeting architecture'}, headers=headers).json()['id']
    planned = wait_state(store, rid, {'awaiting_approval'})
    frozen = planned['context']
    assert any(item['key'] == active['key'] for item in frozen['knowledge'])
    assert all(item['key'] != candidate.json()['key'] for item in frozen['knowledge'])
    assert any(item['content'] == 'Original reviewed guidance.' for item in frozen['knowledge'])
    assert frozen['code']['commit_sha'] == frozen['commit_sha']

    revised = client.put(f"/api/v2/projects/{p['id']}/knowledge/{active['key']}", json={
        **_entry(title='Greeting architecture', content='Later reviewed guidance.'),
        'expected_revision': 1,
    }, headers=headers)
    assert revised.status_code == 200
    assert len(client.get(f"/api/v2/projects/{p['id']}/knowledge/{active['key']}/versions",
                          headers=headers).json()['versions']) == 2
    assert store.get(rid)['context'] == frozen


def test_baseline_drift_refuses_approval(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={'project_id': p['id'],
                                            'request': 'Update greeting safely'}, headers=headers).json()['id']
    planned = wait_state(store, rid, {'awaiting_approval'})
    (repo / 'baseline-only.txt').write_text('drift')
    subprocess.run(['git', 'add', 'baseline-only.txt'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'drift baseline'], cwd=repo, check=True)
    response = client.post(f'/api/v2/runs/{rid}/approve', json={'revision': planned['revision']}, headers=headers)
    assert response.status_code == 409
    assert '基线' in response.text or 'baseline' in response.text.lower()


def test_sync_merge_and_signed_closed_hook_are_idempotent_and_safe(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    merged = _published_run(store, p, number=7)
    unmerged = _published_run(store, p, number=8)
    failed = _published_run(store, p, number=9)
    publisher = FakePublisher({
        7: _merged_evidence(7), 8: {'merged': False, 'reason': 'not_merged', 'pr_number': 8,
                                     'pr_url': 'https://github.com/owner/sample/pull/8'},
        9: httpx.ConnectError('offline'),
    })
    svc.publisher = publisher

    sync = client.post(f"/api/v2/runs/{merged['id']}/sync-merge", headers=headers)
    assert sync.status_code == 200, sync.text
    assert sync.json()['merged'] is True
    assert sync.json()['duplicate'] is False
    repeat = client.post(f"/api/v2/runs/{merged['id']}/sync-merge", headers=headers)
    assert repeat.status_code == 200
    assert repeat.json()['duplicate'] is True
    facts = client.get(f"/api/v2/projects/{p['id']}/knowledge", params={'include_retired': 'true'},
                       headers=headers).json()['entries']
    assert len([entry for entry in facts if entry['kind'] == 'fact']) == 1

    hook_payload = {
        'action': 'closed', 'repository': {'full_name': 'owner/sample'},
        'number': 7,
        'pull_request': {'number': 7, 'state': 'closed', 'merged': True,
                         'html_url': 'https://github.com/owner/sample/pull/7'},
    }
    hook = _signed_webhook(client, hook_payload, delivery='merge-hook-1')
    assert hook.status_code == 200, hook.text
    assert isinstance(hook.json(), dict)
    assert hook.json().get('results', hook.json().get('ignored')) is not None
    facts_after_hook = client.get(f"/api/v2/projects/{p['id']}/knowledge",
                                  params={'include_retired': 'true'}, headers=headers).json()['entries']
    assert len([entry for entry in facts_after_hook if entry['kind'] == 'fact']) == 1

    unmerged_response = client.post(f"/api/v2/runs/{unmerged['id']}/sync-merge", headers=headers)
    assert unmerged_response.status_code == 200
    assert unmerged_response.json()['merged'] is False
    failed_response = client.post(f"/api/v2/runs/{failed['id']}/sync-merge", headers=headers)
    assert failed_response.status_code == 502
    final_facts = client.get(f"/api/v2/projects/{p['id']}/knowledge",
                             params={'include_retired': 'true'}, headers=headers).json()['entries']
    assert len([entry for entry in final_facts if entry['kind'] == 'fact']) == 1

    unmatched = _signed_webhook(client, {
        'action': 'closed', 'repository': {'full_name': 'other/repo'},
        'number': 7,
        'pull_request': {'number': 7, 'state': 'closed', 'merged': True},
    }, delivery='merge-hook-unmatched')
    assert unmatched.status_code == 200
    assert unmatched.json().get('ignored') is True or unmatched.json().get('results') == []


def test_indexed_search_routes(app_env):
    client, store, svc, repo = app_env
    (repo / 'greeting.py').write_text('def greet():\n    return "hello"\n')
    subprocess.run(['git', 'add', 'greeting.py'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'add searchable source'], cwd=repo, check=True)
    headers = login(client)
    p = project(client, repo, headers)
    indexed = client.post(f"/api/v2/projects/{p['id']}/code-index", headers=headers)
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()['indexed'] is True
    search = client.get(f"/api/v2/projects/{p['id']}/code-search",
                        params={'q': 'greet'}, headers=headers)
    assert search.status_code == 200, search.text
    assert search.json()['results']
    graph = client.get(f"/api/v2/projects/{p['id']}/code-graph", headers=headers)
    assert graph.status_code == 200, graph.text
    assert 'nodes' in graph.json() and 'edges' in graph.json()


def test_planning_rejects_dirty_initial_checkout_without_running_provider(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    (repo / 'dirty-before-plan.txt').write_text('uncommitted')
    calls = []
    original = svc.runner.run

    def counted(request, emit, cancel=None):
        calls.append(request)
        return original(request, emit, cancel)

    monkeypatch.setattr(svc.runner, 'run', counted)
    rid = client.post('/api/v2/runs', json={'project_id': p['id'],
                                            'request': 'Plan with a dirty checkout'}, headers=headers).json()['id']
    failed = wait_state(store, rid, {'needs_human'})
    assert failed['status'] == 'needs_human'
    assert calls == []


def test_planning_rejects_checkout_changed_during_provider_readonly_phase(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    original = svc.runner.run

    def mutating(request, emit, cancel=None):
        if request.read_only:
            (repo / 'changed-during-plan.txt').write_text('planner mutation')
        return original(request, emit, cancel)

    monkeypatch.setattr(svc.runner, 'run', mutating)
    rid = client.post('/api/v2/runs', json={'project_id': p['id'],
                                            'request': 'Plan while checkout changes'}, headers=headers).json()['id']
    failed = wait_state(store, rid, {'needs_human'})
    assert failed['status'] == 'needs_human'
    assert any(event['type'] == 'run.failed' for event in store.events(rid))


def test_long_query_history_with_index_keeps_planning_bounded(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    (repo / 'searchable.py').write_text('def searchable():\n    return 1\n')
    subprocess.run(['git', 'add', 'searchable.py'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'add searchable module'], cwd=repo, check=True)
    indexed = client.post(f"/api/v2/projects/{p['id']}/code-index", headers=headers)
    assert indexed.status_code == 200, indexed.text
    run, _ = store.create_run(p['id'], 'q' * 500)
    store.update(run['id'], {'history': [f'history-{i}-' + ('x' * 500) for i in range(250)]},
                 expected=('received',))
    svc.start_plan(run['id'])
    planned = wait_state(store, run['id'], {'awaiting_approval', 'needs_human'})
    assert planned['status'] == 'awaiting_approval', store.events(run['id'])
    assert planned['context']['code']['commit_sha'] == planned['context']['commit_sha']
    assert planned['context']['chars'] <= 12_000


def test_agent_profile_caps_are_bounded_in_frozen_context(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    update = {
        'expected_revision': 1, 'name': 'Large Profile', 'mission': 'm' * 2000,
        'architecture_summary': 'a' * 4000,
        'constraints': [f'constraint-{i}-' + ('c' * 280) for i in range(20)],
    }
    response = client.put(f"/api/v2/projects/{p['id']}/agent", json=update, headers=headers)
    assert response.status_code == 200, response.text
    rid = client.post('/api/v2/runs', json={'project_id': p['id'],
                                            'request': 'Plan with a bounded profile'}, headers=headers).json()['id']
    planned = wait_state(store, rid, {'awaiting_approval'})
    assert planned['context']['agent']['mission'] == 'm' * 700
    assert planned['context']['agent']['architecture_summary'] == 'a' * 1200
    assert len(planned['context']['agent']['constraints']) == 6
