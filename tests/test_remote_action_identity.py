"""An external write must be nameable, queryable, and settled only by real evidence.

The durable row already survived a lost response without replaying the write. What
it could not do was say *which* action it was from outside itself, so nothing could
later go ask the target about it. These tests drive the real `RemoteTargets` against
the fake-ssh target, and for the lost-response case close the first coordinator and
reopen a second `Store`/`Service` over the same `control.db` before reconciling.
"""
from __future__ import annotations

import json

import pytest

from factory.control.remote_targets import action_id
from factory.control.service import Service
from factory.control.store import Conflict, Store
from tests.test_control_app import FakeSDK, app_env, login, project  # noqa: F401
from tests.test_remote_targets import release, remote_env  # noqa: F401


def _writes(calls):
    """Lines the fake ssh recorded that carry the deploy command."""
    if not calls.exists():
        return []
    return [line for line in calls.read_text().splitlines() if '/srv/deploy' in line]


def _reopened(tmp_path, monkeypatch):
    """A second coordinator over the same durable file, as a real restart is."""
    store = Store(tmp_path / 'data' / 'control.db')
    svc = Service(store, runner=FakeSDK(),
                  profiles={role: {'provider': 'codex', 'model': 'test'}
                            for role in ('planner', 'cheap', 'standard', 'strong')})
    monkeypatch.setattr(svc, '_ensure_scheduler', lambda: None)
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: None)
    return store, svc


def _row(store, rid, target_id, verb='deploy'):
    with store.connect() as db:
        row = db.execute('SELECT action_id,intent,at,result FROM remote_invocations '
                         'WHERE run_id=? AND target_id=? AND verb=?',
                         (rid, target_id, verb)).fetchone()
    return None if row is None else dict(row)


def test_write_is_claimed_with_a_stable_id_and_intent(remote_env):
    """The id is derived, so the same action has the same name on every look."""
    _, store, service, _, _, target, _, calls = remote_env
    run = release(remote_env)
    result = service.remote.execute(run['id'], target['id'], 'deploy')
    expected = action_id(run['id'], target['id'], 'deploy')
    assert result['status'] == 'pass' and result['action_id'] == expected
    assert len(_writes(calls)) == 1
    row = _row(store, run['id'], target['id'])
    assert row['action_id'] == expected and row['at']
    intent = json.loads(row['intent'])
    assert intent['verb'] == 'deploy' and intent['host'] == target['host']
    assert intent['target_revision'] == target['revision']
    # The id is a function of what identifies the action, not of when it ran.
    assert action_id(run['id'], target['id'], 'deploy') == expected
    assert action_id(run['id'], target['id'], 'rollback') != expected


def test_repeat_returns_the_original_receipt_without_a_second_write(remote_env):
    _, store, service, _, _, target, _, calls = remote_env
    run = release(remote_env)
    first = service.remote.execute(run['id'], target['id'], 'deploy')
    again = service.remote.execute(run['id'], target['id'], 'deploy')
    assert again == first
    assert len(_writes(calls)) == 1


def test_same_key_with_different_parameters_conflicts(remote_env):
    """A different action wearing this one's name gets neither the receipt nor a rerun.

    Reusing the stored receipt would report the earlier action's outcome for this
    one; running it would be a second external write. The row is claimed here with
    the parameters of an earlier target configuration, which is the shape a
    same-key-different-action collision actually takes.
    """
    _, store, service, _, _, target, _, calls = remote_env
    run = release(remote_env)
    aid = action_id(run['id'], target['id'], 'deploy')
    stale = {'target_id': target['id'], 'target': target['name'], 'verb': 'deploy',
             'host': 'old.example.test', 'port': 22, 'user': 'deploy',
             'target_revision': target['revision']}
    pending = {'action_id': aid, 'intent': stale, 'target_id': target['id'],
               'target': target['name'], 'verb': 'deploy', 'status': 'unverified',
               'exit_code': None, 'executed': False, 'duration_s': 0, 'reason': '未收到回执'}
    with store.connect() as db:
        db.execute('INSERT INTO remote_invocations(run_id,target_id,verb,result,action_id,intent,at)'
                   ' VALUES(?,?,?,?,?,?,?)',
                   (run['id'], target['id'], 'deploy', json.dumps(pending), aid,
                    json.dumps(stale, sort_keys=True), '2026-09-21T00:00:00+00:00'))
    with pytest.raises(Conflict) as caught:
        service.remote.execute(run['id'], target['id'], 'deploy')
    assert caught.value.error_type == 'remote_action_conflict'
    assert not _writes(calls)


def _lost_response(store, run, target):
    """The row a crash between claiming and receiving the receipt leaves behind.

    The external write did land; only the answer was lost. Nothing local can tell
    that apart from a write that never happened, which is why the row must stay
    unverified until the target is asked.
    """
    from factory.control.remote_targets import intent_of
    aid = action_id(run['id'], target['id'], 'deploy')
    intent = intent_of(target, 'deploy')
    pending = {'action_id': aid, 'intent': intent, 'target_id': target['id'],
               'target': target['name'], 'verb': 'deploy', 'status': 'unverified',
               'exit_code': None, 'executed': False, 'duration_s': 0,
               'reason': '动作已登记但未收到完成回执；请人工核对，不自动重放'}
    with store.connect() as db:
        db.execute('INSERT INTO remote_invocations(run_id,target_id,verb,result,action_id,intent,at)'
                   ' VALUES(?,?,?,?,?,?,?)',
                   (run['id'], target['id'], 'deploy', json.dumps(pending), aid,
                    json.dumps(intent, sort_keys=True), '2026-09-21T00:00:00+00:00'))
    return aid, pending


def test_lost_response_is_settled_after_reopening_by_target_reported_action(
        remote_env, tmp_path, monkeypatch):
    """Reconciliation runs on a second coordinator and only a read-only verb runs."""
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, pending = _lost_response(store, run, target)
    client.close()

    # The target is able to name the action it applied.
    data = json.loads(config.read_text())
    config.write_text(json.dumps({**data, 'output': 'active (running)\napplied=' + aid}))
    store2, svc2 = _reopened(tmp_path, monkeypatch)
    # The reopened coordinator finds the claim, by id, with no I/O.
    listed = svc2.remote.actions(action_id=aid)
    assert [item['status'] for item in listed] == ['unverified']
    assert listed[0]['intent']['verb'] == 'deploy' and not calls.exists()

    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'pass'
    assert settled['reconciled'] == 'target_reported_action'
    assert aid in settled['reconcile_evidence']
    # Durable, and still not a second write: only status-service ran.
    assert _row(store2, run['id'], target['id'])['result'] == json.dumps(settled)
    assert not _writes(calls)
    assert [json.loads(line)[-1] for line in calls.read_text().splitlines()] == ['status-service']
    assert svc2.remote.actions(run['id'])[0]['status'] == 'pass'


def test_healthy_target_that_cannot_name_the_action_stays_unknown(
        remote_env, tmp_path, monkeypatch):
    """A green probe says something is up; it does not say this write put it there."""
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, pending = _lost_response(store, run, target)
    client.close()

    # Default fake target answers 'healthy' with exit code 0 and names nothing.
    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified'
    assert settled['reconciled'] == 'unknown_target_cannot_confirm'
    assert settled['action_id'] == aid
    assert not _writes(calls)
    # A health check passing afterwards must not upgrade it either.
    assert svc2.remote.execute(run['id'], target['id'], 'health_check')['status'] == 'pass'
    assert svc2.remote.actions(action_id=aid)[0]['status'] == 'unverified'


def test_unreachable_target_stays_pending_and_no_write_is_resent(
        remote_env, tmp_path, monkeypatch):
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, pending = _lost_response(store, run, target)
    client.close()

    data = json.loads(config.read_text())
    config.write_text(json.dumps({**data, 'output': 'ssh: connect failed', 'exit_code': 255}))
    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified'
    assert settled['reconciled'] == 'unknown_target_cannot_confirm'
    assert not _writes(calls)
    # Reconciling again is still read-only, and still refuses to guess.
    assert svc2.remote.reconcile(run['id'], target['id'], 'deploy')['status'] == 'unverified'
    assert not _writes(calls)
