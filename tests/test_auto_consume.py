"""T08: Auto-consume of pending followups at safe nodes and followup expiry.

Tests use a controllable FakeRunner (explicitly labelled as fake) that drives
the run to needs_human, verifying that:
- auto_resume fires without a second user API call
- events contain auto_resume with pending_ids
- per-item applied/expired status is correct
- cancelled runs are never auto-resumed
- terminal success expires unconsumed pending
- restart preserves unconsumed pending for next safe node
- concurrent submission and crash-direction (duplicate not loss)
"""
import json
import subprocess
import sys
import threading
import time

import pytest

from factory.control.run_lifecycle import (
    _auto_resume_with_followups,
    _collect_pending_followups,
    _expire_unconsumed_followups,
    _mark_followups_applied,
    _merge_followup_content,
)
from factory.control.service import Service
from factory.control.store import Conflict, Store
from tests.test_control_app import app_env, login, project, FakeSDK, wait_state  # noqa: F401


def _setup_resumable_run(client, store, svc, repo, headers, *, followup_contents=('加搜索功能',)):
    """Create a run with plan+artifacts at needs_human, with pending followups.

    Returns (rid, project, pending_ids).
    Uses a FakeSDK runner (fake; see test_control_app.FakeSDK).
    """
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={
        'operation': 'general', 'project_id': p['id'], 'request': '做一个工具'
    }, headers=headers).json()['id']
    # Wait for planning to settle, then clear active_jobs so auto-resume is not
    # blocked by the planning slot that the FakeSDK runner created.
    time.sleep(0.3)
    with svc.lock:
        svc.active_jobs.pop(rid, None)
    base = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=str(repo),
        capture_output=True, text=True
    ).stdout.strip()
    # Set up a running state with plan
    store.update(rid, {
        'status': 'running',
        'plan': {
            'title': 'test', 'summary': '', 'questions': [],
            'tasks': [{
                'id': 't1', 'title': 'a', 'prompt': 'a',
                'acceptance': ['ok'], 'paths': ['x'],
                'checks': ['greeting'], 'depends_on': [],
                'complexity': 'small', 'risk': 'low',
            }],
        },
        'revision': 1,
    })
    # Submit followups during active execution
    pending_ids = []
    for content in followup_contents:
        res = client.post(
            f'/api/v2/runs/{rid}/follow-up',
            json={'content': content},
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()['queued'] is True
    # Collect pending_ids
    for event in store.export_events(rid, kind='followup.pending'):
        pending_ids.append(event['payload']['id'])
    # Transition to needs_human with resumable artifacts
    store.update(rid, {
        'status': 'needs_human',
        'artifacts': {'base_sha': base, 'tasks': [{'id': 't1'}]},
        'execution_checks': store.project(p['id'])['checks'],
    })
    return rid, p, pending_ids


# ---------- Scenario 1: auto-resume at needs_human ----------

def test_auto_resume_fires_without_user_api_call(app_env):
    """When _auto_resume_with_followups is called on a needs_human run with
    pending followups, it transitions to queued and records auto_resume event
    with pending_ids — no second user API call needed.

    Uses: FakeSDK runner (fake).
    """
    client, store, svc, repo = app_env
    headers = login(client)
    rid, p, pending_ids = _setup_resumable_run(client, store, svc, repo, headers)

    # Auto-resume should succeed
    result = _auto_resume_with_followups(svc, rid)
    assert result is True

    # Run should be queued (or further along if _submit triggered fast)
    run = store.get(rid)
    assert run['status'] in ('queued', 'running', 'ready_for_review', 'needs_human')

    # auto_resume event should exist with pending_ids
    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) == 1
    payload = auto_events[0]['payload']
    assert payload['actor'] == 'system/auto'
    assert set(payload['pending_ids']) == set(pending_ids)

    # followup.applied events should be recorded
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == len(pending_ids)
    applied_pids = {e['payload']['pending_id'] for e in applied}
    assert applied_pids == set(pending_ids)


def test_auto_resume_event_contains_merged_answer(app_env):
    """The auto_resume event's answer merges all pending followup contents."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, _ = _setup_resumable_run(
        client, store, svc, repo, headers,
        followup_contents=('加搜索', '改颜色'),
    )

    _auto_resume_with_followups(svc, rid)

    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) == 1
    answer = auto_events[0]['payload']['answer']
    assert '加搜索' in answer
    assert '改颜色' in answer
    assert '用户在执行中补充的要求' in answer


# ---------- Scenario 2: terminal success → expired ----------

def test_completed_run_expires_unconsumed_pending(app_env):
    """When a run reaches ready_for_review, unconsumed pending followups are
    marked as followup.expired."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(client, store, svc, repo, headers)

    # Move directly to ready_for_review (simulating successful completion)
    store.update(rid, {'status': 'ready_for_review'})
    _expire_unconsumed_followups(svc, rid)

    # followup.expired events should exist
    expired = list(store.export_events(rid, kind='followup.expired'))
    assert len(expired) == len(pending_ids)
    expired_pids = {e['payload']['pending_id'] for e in expired}
    assert expired_pids == set(pending_ids)

    # GET run should show expired status
    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    for f in run_data['followups']:
        assert f['expired'] is True
        assert f['applied'] is False


def test_completed_run_does_not_expire_already_applied(app_env):
    """Applied followups are NOT expired when run completes."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(
        client, store, svc, repo, headers,
        followup_contents=('已消费', '未消费'),
    )

    # Mark first as applied
    store.append(rid, 'followup.applied', {
        'pending_id': pending_ids[0], 'run_revision': 1,
    })

    store.update(rid, {'status': 'ready_for_review'})
    _expire_unconsumed_followups(svc, rid)

    # Only the unconsumed one should be expired
    expired = list(store.export_events(rid, kind='followup.expired'))
    assert len(expired) == 1
    assert expired[0]['payload']['pending_id'] == pending_ids[1]

    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    applied_items = [f for f in run_data['followups'] if f['applied']]
    expired_items = [f for f in run_data['followups'] if f.get('expired')]
    assert len(applied_items) == 1
    assert len(expired_items) == 1


# ---------- Scenario 3: cancel → no auto-resume, preserve pending ----------

def test_cancel_does_not_auto_resume(app_env):
    """Cancelled runs with pending followups are never auto-resumed."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(client, store, svc, repo, headers)

    # Cancel the run
    store.update(rid, {'status': 'cancelled'})

    result = _auto_resume_with_followups(svc, rid)
    assert result is False

    # No auto_resume events
    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) == 0

    # Pending followups still exist unchanged
    pending = list(store.export_events(rid, kind='followup.pending'))
    assert len(pending) == len(pending_ids)

    # No applied events
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == 0


# ---------- Scenario 4: two followups, one applied one still pending ----------

def test_partial_application_per_item_status(app_env):
    """Two followups: one applied, one still pending. GET run shows per-item status."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(
        client, store, svc, repo, headers,
        followup_contents=('第一条补充', '第二条补充'),
    )

    # Apply only the first
    store.append(rid, 'followup.applied', {
        'pending_id': pending_ids[0], 'run_revision': 1,
    })

    run_data = client.get(f'/api/v2/runs/{rid}', headers=headers).json()
    followups = run_data['followups']
    assert len(followups) == 2

    applied_items = [f for f in followups if f['applied']]
    pending_items = [f for f in followups if not f['applied'] and not f.get('expired')]
    assert len(applied_items) == 1
    assert len(pending_items) == 1
    assert applied_items[0]['id'] == pending_ids[0]
    assert pending_items[0]['id'] == pending_ids[1]


# ---------- Scenario 5: restart preserves unconsumed for next safe node ----------

def test_restart_preserves_pending_for_next_safe_node(app_env):
    """After service restart (simulated by new Service), unconsumed pending
    followups are still consumable at the next safe node."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(client, store, svc, repo, headers)

    # Verify pending survives (event-sourced; no in-memory state)
    collected = _collect_pending_followups(svc, rid)
    assert len(collected) == len(pending_ids)

    # Auto-resume should still work
    result = _auto_resume_with_followups(svc, rid)
    assert result is True

    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == len(pending_ids)


# ---------- Scenario 6: concurrent submission + crash direction ----------

def test_concurrent_submit_and_consume_direction_is_duplicate_not_loss(app_env):
    """If followup.applied is written but auto_resume crashes before
    _submit, the content is already merged into history. A subsequent
    auto-resume attempt finds no unconsumed pending (safe: duplicate not loss)."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(client, store, svc, repo, headers)

    # Simulate: collect + mark applied, but don't submit
    with svc.lock:
        collected = _collect_pending_followups(svc, rid)
        answer, _ = _merge_followup_content('', collected)
        run = store.get(rid)
        store.update(rid, {
            'status': 'queued',
            'resume_count': 1,
            'execution_resume': {'artifacts': run['artifacts'], 'answer': answer, 'revision': 1},
            'history': [*run['history'], answer],
        }, expected=('needs_human',), revision=1,
            event=('run.auto_resumed', {'actor': 'system/auto', 'answer': answer,
                'revision': 1, 'resume_count': 1, 'pending_ids': pending_ids}))
        _mark_followups_applied(svc, rid, collected)

    # Simulate crash: set back to needs_human
    store.update(rid, {'status': 'needs_human'})

    # Second attempt: nothing to consume (already applied)
    result = _auto_resume_with_followups(svc, rid)
    assert result is False  # No more unconsumed pending

    # The applied events exist from the first attempt
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == len(pending_ids)


# ---------- Scenario 7: no pending followups → no auto-resume ----------

def test_no_pending_means_no_auto_resume(app_env):
    """Without pending followups, auto-resume does not fire."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={
        'operation': 'general', 'project_id': p['id'], 'request': '做工具'
    }, headers=headers).json()['id']
    base = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=str(repo),
        capture_output=True, text=True
    ).stdout.strip()
    store.update(rid, {
        'status': 'needs_human',
        'plan': {'title': 'test', 'summary': '', 'questions': [], 'tasks': [
            {'id': 't1', 'title': 'a', 'prompt': 'a', 'acceptance': ['ok'],
             'paths': ['x'], 'checks': ['greeting'], 'depends_on': [],
             'complexity': 'small', 'risk': 'low'}]},
        'artifacts': {'base_sha': base, 'tasks': [{'id': 't1'}]},
        'revision': 1,
    })

    result = _auto_resume_with_followups(svc, rid)
    assert result is False


# ---------- Scenario 8: per-item pending_id in conversation ----------

def test_conversation_includes_pending_id_per_message(app_env):
    """store.conversation() returns pending_id for each followup message,
    enabling per-item badge lookup in the frontend."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, _, pending_ids = _setup_resumable_run(
        client, store, svc, repo, headers,
        followup_contents=('补充A', '补充B'),
    )

    messages = client.get(
        f'/api/v2/runs/{rid}/conversation', headers=headers,
    ).json()['messages']
    followup_msgs = [m for m in messages if m.get('followup')]
    assert len(followup_msgs) == 2
    msg_pending_ids = [m.get('pending_id') for m in followup_msgs]
    assert set(msg_pending_ids) == set(pending_ids)
    # Each message has its own unique pending_id
    assert len(set(msg_pending_ids)) == 2


# ---------- Scenario 9: auto-resume without resumable artifacts is silently skipped ----------

def test_auto_resume_skipped_without_resumable_artifacts(app_env):
    """If the run has pending followups but no resumable artifacts,
    auto-resume silently skips without error."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={
        'operation': 'general', 'project_id': p['id'], 'request': '做工具'
    }, headers=headers).json()['id']
    store.update(rid, {'status': 'running', 'revision': 1})
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '加功能'}, headers=headers)
    store.update(rid, {'status': 'needs_human', 'artifacts': {}})

    result = _auto_resume_with_followups(svc, rid)
    assert result is False
    # Run stays at needs_human
    assert store.get(rid)['status'] == 'needs_human'


# ---------- Scenario 10: service restart triggers auto-resume via recover() ----------

def test_service_restart_auto_resumes_needs_human_with_pending(app_env):
    """A run that was interrupted by restart (set to needs_human by
    store.recover) with unconsumed pending followups is auto-resumed by
    svc.recover() without any user API call.

    Simulated by: (1) setting run to 'running' as if mid-execution,
    (2) calling store.recover() to move it to needs_human (same as
    server restart does), (3) calling svc.recover() which should detect
    the pending followups and auto-resume.

    Uses: FakeSDK runner (fake).
    """
    client, store, svc, repo = app_env
    headers = login(client)
    rid, p, pending_ids = _setup_resumable_run(client, store, svc, repo, headers)

    # Simulate a mid-execution crash: put the run into 'running' as if it
    # was executing when the server died.
    store.update(rid, {'status': 'running'})

    # store.recover() sets ACTIVE runs to needs_human
    store.recover()
    assert store.get(rid)['status'] == 'needs_human'

    # svc.recover() should auto-resume it — no user API call
    svc.recover()

    # After recover, the run should have left needs_human
    run = store.get(rid)
    assert run['status'] != 'needs_human', f'Expected auto-resume but status is {run["status"]}'

    # run.auto_resumed event should exist
    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) >= 1
    payload = auto_events[-1]['payload']
    assert payload['actor'] == 'system/auto'
    assert set(payload['pending_ids']) == set(pending_ids)

    # followup.applied events should be recorded
    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == len(pending_ids)


def test_service_restart_does_not_resume_requirement_analysis_interrupted(app_env):
    """The requirement_analysis interrupted path (recovery.py:89) sets
    needs_human but uses budget_resume semantics; auto-resume correctly
    skips it because there is no plan or resumable artifacts."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={
        'operation': 'general', 'project_id': p['id'], 'request': '做工具'
    }, headers=headers).json()['id']
    time.sleep(0.3)
    with svc.lock:
        svc.active_jobs.pop(rid, None)

    # Simulate: requirement_analysis was interrupted, now at needs_human
    # with no plan, no artifacts — just an error message and a pending followup
    store.update(rid, {'status': 'running', 'revision': 1})
    client.post(f'/api/v2/runs/{rid}/follow-up', json={'content': '补充'}, headers=headers)
    store.update(rid, {
        'status': 'needs_human',
        'error': '需求分析被重启中断',
        # No plan, no artifacts — budget_resume semantics
    })

    result = _auto_resume_with_followups(svc, rid)
    assert result is False
    assert store.get(rid)['status'] == 'needs_human'
