"""Independent regression probes: real lifecycle/HTTP; no paid provider calls."""
import threading
from factory.control import effective_contract as ec
from factory.control import run_execution
from tests.test_control_app import app_env, login
from tests.test_effective_contract import _confirmed, _resumable


def test_unsupported_analyst_cannot_receipt_unread_scope_as_applied(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p, rid = _confirmed(client, store, repo, headers)
    response = client.post(f'/api/v2/runs/{rid}/follow-up', headers=headers,
        json={'content': '我确认新增 square 功能', 'idempotency_key': 'no-analyst'})
    assert response.status_code == 200
    _resumable(store, p, rid, repo)
    cfg = store.get(rid)['runtime_configuration']
    store.update(rid, {'runtime_configuration': {**cfg,
        'agent_verification_profile': {'provider': 'codex', 'model': 'test'}}})
    submitted = []
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: submitted.append(a))
    response = client.post(f'/api/v2/runs/{rid}/continue', headers=headers,
        json={'answer': '', 'revision': 1, 'resume_count': 0})
    assert not list(store.export_events(rid, kind='followup.applied')), response.text
    assert not submitted
    assert response.status_code == 409
    assert ec.revision_of(store.get(rid)) == 1


def test_real_run_cannot_expire_input_accepted_between_guard_and_delivery(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p, rid = _confirmed(client, store, repo, headers, status='queued')
    svc.cancels[rid] = threading.Event()
    monkeypatch.setattr(svc, 'execute', lambda **kw: {
        'base_sha': 'test-base', 'tasks': [{'id': 't1', 'status': 'completed'}]})
    def verify(rid, run, project, config, artifacts):
        artifacts.update(verification_effective_revision=1,
                         verification={'verdict': 'pass', 'criteria': []})
    monkeypatch.setattr(svc, '_independent_verify', verify)
    monkeypatch.setattr(svc, '_capture_capability', lambda *a: None)
    original = store.update
    reached, received, replies, workers = threading.Event(), threading.Event(), [], []
    def post_followup():
        try:
            replies.append(client.post(f'/api/v2/runs/{rid}/follow-up', headers=headers,
                json={'content': '还需要 CSV 导出', 'idempotency_key': 'landing-race'}))
        finally:
            received.set()
    def interleave(run_id, changes, *args, **kwargs):
        if run_id == rid and changes.get('status') == 'ready_for_review':
            reached.set()
            t = threading.Thread(target=post_followup)
            workers.append(t)
            t.start()
            received.wait(2)  # Correct atomic landing may hold the shared lock.
        return original(run_id, changes, *args, **kwargs)
    monkeypatch.setattr(store, 'update', interleave)
    run_execution._run(svc, rid)
    for t in workers:
        t.join(5)
        assert not t.is_alive()
    assert reached.is_set(), store.get(rid)
    assert len(replies) == 1
    accepted = replies[0].status_code == 200
    expired = list(store.export_events(rid, kind='followup.expired'))
    assert not (accepted and expired and store.get(rid)['status'] == 'ready_for_review'), (
        'An accepted requirement was expired as the old delivery completed', replies[0].text, expired)
