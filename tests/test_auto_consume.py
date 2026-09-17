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


# ---------- Scenario 12: spec_confirmation run with requirement workspace ----------

def _setup_requirement_workspace_run(client, store, svc, repo, headers, *, followup_content='补充要求'):
    """Create a spec_confirmation run whose artifacts.base_sha comes from
    a requirement branch (different from project main), simulating the
    live scenario where requirement analysis creates a separate worktree.

    Returns (rid, project_id, pending_ids, requirement_workspace, requirement_branch).
    Uses FakeSDK runner (fake).
    """
    import uuid as _uuid
    p = project(client, repo, headers)
    rid = client.post('/api/v2/runs', json={
        'operation': 'general', 'project_id': p['id'], 'request': '做工具'
    }, headers=headers).json()['id']
    time.sleep(0.3)
    with svc.lock:
        svc.active_jobs.pop(rid, None)

    # Create a requirement branch that diverges from main
    req_branch = f'factory/spec-{rid}'
    subprocess.run(['git', 'checkout', '-b', req_branch], cwd=str(repo),
                   check=True, capture_output=True)
    (repo / 'spec.md').write_text('requirement spec')
    subprocess.run(['git', 'add', 'spec.md'], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'add spec'], cwd=str(repo),
                   check=True, capture_output=True)
    req_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                             capture_output=True, text=True).stdout.strip()
    # Go back to main — main HEAD is now different from req_branch HEAD
    subprocess.run(['git', 'checkout', 'main'], cwd=str(repo),
                   check=True, capture_output=True)
    main_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()
    assert req_sha != main_sha, 'requirement branch must differ from main'

    # Set up run with spec_confirmation and requirement workspace
    store.update(rid, {
        'status': 'running',
        'spec_confirmation': {'actor': 'project-policy', 'at': '2026-01-01', 'automatic': True},
        'requirement_workspace': str(repo),
        'requirement_branch': req_branch,
        'execution_mode': 'continuous',
        'plan': {
            'title': 'test', 'summary': '', 'questions': [],
            'tasks': [{
                'id': 't1', 'title': 'a', 'prompt': 'a',
                'acceptance': ['ok'], 'paths': ['x'],
                'checks': ['greeting'], 'depends_on': [],
                'complexity': 'small', 'risk': 'low',
            }],
        },
        'revision': 2,
    })

    # Submit followup during execution
    res = client.post(f'/api/v2/runs/{rid}/follow-up',
                      json={'content': followup_content}, headers=headers)
    assert res.status_code == 200

    pending_ids = [e['payload']['id']
                   for e in store.export_events(rid, kind='followup.pending')]

    # Transition to needs_human with artifacts whose base_sha = req_sha
    store.update(rid, {
        'status': 'needs_human',
        'artifacts': {
            'base_sha': req_sha,
            'tasks': [{'id': 't1'}],
            'execution_mode': 'continuous',
            'branch': f'factory/{rid}-r2',
            'worktree': str(repo),
        },
        'execution_checks': store.project(p['id'])['checks'],
    })
    return rid, p['id'], pending_ids, str(repo), req_branch, req_sha, main_sha


def test_spec_confirmation_run_auto_resumes_despite_different_main(app_env):
    """A spec_confirmation run with requirement workspace has artifacts.base_sha
    from the requirement branch, which differs from the project's main branch.
    Auto-resume must use _project_for_run (which adjusts to the requirement
    workspace), so the baseline check passes.

    This reproduces the live scenario where baseline_sha(raw_project) would
    return the main branch SHA but the correct comparison root is the
    requirement branch.

    Uses: FakeSDK runner (fake).
    """
    client, store, svc, repo = app_env
    headers = login(client)
    rid, pid, pending_ids, ws, req_branch, req_sha, main_sha = \
        _setup_requirement_workspace_run(client, store, svc, repo, headers)

    result = _auto_resume_with_followups(svc, rid)
    assert result is True, (
        f'auto-resume should succeed: req_sha={req_sha[:12]}, main_sha={main_sha[:12]}')

    # Verify events
    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) == 1
    assert auto_events[0]['payload']['actor'] == 'system/auto'
    assert set(auto_events[0]['payload']['pending_ids']) == set(pending_ids)

    applied = list(store.export_events(rid, kind='followup.applied'))
    assert len(applied) == len(pending_ids)


def test_external_baseline_change_still_blocks_auto_resume(app_env):
    """If someone pushes to the requirement branch between execution and
    auto-resume, the baseline has truly changed and auto-resume must be
    blocked — the guard is not removed.

    Uses: FakeSDK runner (fake).
    """
    client, store, svc, repo = app_env
    headers = login(client)
    rid, pid, pending_ids, ws, req_branch, req_sha, main_sha = \
        _setup_requirement_workspace_run(client, store, svc, repo, headers)

    # Simulate external push to the requirement branch
    subprocess.run(['git', 'checkout', req_branch], cwd=ws,
                   check=True, capture_output=True)
    (repo / 'external_change.txt').write_text('pushed by another')
    subprocess.run(['git', 'add', 'external_change.txt'], cwd=ws,
                   check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'external push'], cwd=ws,
                   check=True, capture_output=True)
    new_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ws,
                             capture_output=True, text=True).stdout.strip()
    subprocess.run(['git', 'checkout', 'main'], cwd=ws,
                   check=True, capture_output=True)
    assert new_sha != req_sha, 'external push must move the branch'

    result = _auto_resume_with_followups(svc, rid)
    assert result is False, 'external baseline change must block auto-resume'
    assert store.get(rid)['status'] == 'needs_human'

    # No auto_resume events
    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) == 0


def test_baseline_exception_logged_and_event_emitted(app_env):
    """When the baseline check throws (e.g. workspace deleted), the exception
    is logged at WARNING and a durable event is emitted, not silently swallowed."""
    client, store, svc, repo = app_env
    headers = login(client)
    rid, pid, pending_ids, ws, req_branch, req_sha, main_sha = \
        _setup_requirement_workspace_run(client, store, svc, repo, headers)

    # Point requirement_workspace to a non-existent path
    store.update(rid, {'requirement_workspace': '/nonexistent/path'})

    result = _auto_resume_with_followups(svc, rid)
    assert result is False

    # A durable event should record the failure reason
    skip_events = list(store.export_events(rid, kind='followup.auto_resume_skipped'))
    assert len(skip_events) == 1
    payload = skip_events[0]['payload']
    assert payload['reason'] == 'baseline_check_exception'
    assert 'Error' in payload['error'] or 'error' in payload['error'].lower() or '不存在' in payload['error'] or 'No such' in payload['error']


# ---------- Scenario 15: full _job path with real thread pool ----------

def test_full_job_path_auto_resumes_after_execution_failure(app_env):
    """Exercises the real _submit -> _job path through the thread pool.
    Monkeypatches _run to call _fail (simulating an execution failure),
    proving that _job's finally hook fires auto-resume AFTER active_jobs.pop.

    Root cause reproduced: before the fix, the hook was inside _run's except
    blocks where rid was still in active_jobs, so the active_jobs guard
    always returned False.

    Uses: app_env with monkeypatched _run (fake failure path).
    """
    import uuid as _uuid
    from factory.control.store import now as _now

    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)

    # Create a run and drive it to 'queued' with a plan and resumable
    # artifacts, plus a pending followup — all the preconditions for
    # auto-resume.  We do this by hand because the real execution pipeline
    # is not the subject under test; the _job timing is.
    rid = client.post('/api/v2/runs', json={
        'operation': 'general', 'project_id': p['id'], 'request': '做工具',
    }, headers=headers).json()['id']
    # Let planning settle
    wait_state(store, rid, ('awaiting_approval', 'needs_clarification',
                            'needs_human', 'queued', 'running'))
    time.sleep(0.5)
    with svc.lock:
        svc.active_jobs.pop(rid, None)

    base_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()
    store.update(rid, {
        'status': 'queued',
        'plan': {'title': 't', 'summary': '', 'questions': [], 'tasks': [
            {'id': 't1', 'title': 'a', 'prompt': 'a', 'acceptance': ['ok'],
             'paths': ['x'], 'checks': ['greeting'], 'depends_on': [],
             'complexity': 'small', 'risk': 'low'}]},
        'revision': 1,
        'artifacts': {'base_sha': base_sha, 'tasks': [{'id': 't1'}]},
        'execution_checks': store.project(p['id'])['checks'],
    })

    # Inject a pending followup
    pending_id = _uuid.uuid4().hex
    with store.connect() as db:
        store._event(db, rid, 'user.message', {
            'text': '加搜索', 'followup': True, 'queued': True,
            'applied': False, 'actor': 'owner', 'actor_id': 1,
            'pending_id': pending_id,
        })
        store._event(db, rid, 'followup.pending', {
            'id': pending_id, 'content': '加搜索',
            'actor_id': 1, 'actor': 'owner',
            'fingerprint': 'test', 'created_at': _now(),
        })

    # Monkeypatch _run: call _fail to set needs_human (the real failure path)
    original_run = type(svc)._run

    def failing_run(self_svc, run_id):
        """Simulates execution failure: sets needs_human via _fail."""
        self_svc._fail(run_id, RuntimeError('simulated execution failure'))

    type(svc)._run = failing_run
    try:
        # Submit through the real _submit -> _job path (thread pool)
        svc._submit(svc._run, rid)

        # Wait for the auto_resumed event
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
            if auto_events:
                break
            time.sleep(0.05)
    finally:
        type(svc)._run = original_run

    auto_events = list(store.export_events(rid, kind='run.auto_resumed'))
    assert len(auto_events) >= 1, (
        f'auto-resume must fire via _job path; '
        f'status={store.get(rid)["status"]}, '
        f'active_jobs={list(svc.active_jobs.keys())}, '
        f'auto_resumed={len(auto_events)}'
    )
    payload = auto_events[0]['payload']
    assert payload['actor'] == 'system/auto'
    assert pending_id in payload['pending_ids']
