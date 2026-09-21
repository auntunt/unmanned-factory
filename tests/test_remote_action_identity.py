"""An external write must be nameable, queryable, and settled only by real evidence.

The durable row already survived a lost response without replaying the write. What
it could not do was say *which* action it was from outside itself, so nothing could
later go ask the target about it. These tests drive the real `RemoteTargets` against
the fake-ssh target, and for the lost-response case close the first coordinator and
reopen a second `Store`/`Service` over the same `control.db` before reconciling.
"""
from __future__ import annotations

import json
import sys

import pytest

from factory.control.remote_targets import (
    ACTION_ENV, INTENT_ENV, QUERY_ENV, RECEIPT_MARKER, RECEIPT_SCHEMA, TARGET_ENV,
    VERB_ENV, action_id, intent_digest, intent_of)
from factory.control.service import Service
from factory.control.store import Conflict, Store
from tests.test_control_app import FakeSDK, app_env, login, project  # noqa: F401
from tests.test_remote_targets import release, remote_env  # noqa: F401


def _query_command(aid, target, *, verb='deploy'):
    """The exact read-only command a reconciliation should send, identity and all.

    Spelled out rather than built from the production helper: a test that asked
    `_with_identity` to describe itself would agree with any ordering or any set
    of variables it happened to send, including none.
    """
    return (f'{INTENT_ENV}={intent_digest(intent_of(target, verb))} '
            f'{QUERY_ENV}={aid} {TARGET_ENV}={target["id"]} {VERB_ENV}={verb} '
            'status-service')


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

    # The target answers with the agreed receipt: schema, this action, this
    # target, this verb, the frozen intent it was claimed under, and a state.
    data = json.loads(config.read_text())
    receipt = RECEIPT_MARKER + json.dumps({
        'schema': RECEIPT_SCHEMA, 'action_id': aid, 'target_id': target['id'],
        'verb': 'deploy', 'intent_digest': intent_digest(intent_of(target, 'deploy')),
        'state': 'applied'})
    config.write_text(json.dumps({**data, 'output': 'active (running)\n' + receipt}))
    store2, svc2 = _reopened(tmp_path, monkeypatch)
    # The reopened coordinator finds the claim, by id, with no I/O.
    listed = svc2.remote.actions(action_id=aid)
    assert [item['status'] for item in listed] == ['unverified']
    assert listed[0]['intent']['verb'] == 'deploy' and not calls.exists()

    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'pass'
    assert settled['reconciled'] == 'target_reported_action'
    assert settled['receipt']['state'] == 'applied'
    assert aid in settled['reconcile_evidence']
    # Durable, and still not a second write: only status-service ran, with the
    # action id carried as a query so the target knows which one is being asked
    # about. The registered command text itself is unchanged.
    assert _row(store2, run['id'], target['id'])['result'] == json.dumps(settled)
    assert not _writes(calls)
    commands = [json.loads(line)[-1] for line in calls.read_text().splitlines()]
    assert commands == [_query_command(aid, target)]
    assert svc2.remote.actions(run['id'])[0]['status'] == 'pass'


def _cooperating_target(tmp_path, config):
    """Rewrite the fake ssh so the target itself keeps a receipt ledger.

    The point of these cases is that the identity actually crosses the wire. So
    this script reads the agreed environment off the command it was handed, files
    a receipt of its own when it performs a write, and on a query prints back the
    receipt it filed earlier. Nothing here is fed the answer by the test.
    """
    ledger = tmp_path / 'target-receipts.jsonl'
    script = f'''#!{sys.executable}
import json, os, re, sys
from pathlib import Path
config = json.loads(Path({str(config)!r}).read_text())
if Path(sys.argv[0]).name == 'ssh-keyscan':
    print('example.test ' + config['key']); sys.exit(0)
with open({str(tmp_path / 'ssh-calls.jsonl')!r}, 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
ledger = Path({str(ledger)!r})
# The coordinator prefixes `KEY=value` assignments onto the registered command.
env = dict(re.findall(r'(WEBUDDY_[A-Z_]+)=(\\S+)', sys.argv[-1]))
marker = {RECEIPT_MARKER!r}
def receipt(state, aid):
    return marker + json.dumps({{'schema': {RECEIPT_SCHEMA!r}, 'action_id': aid,
        'target_id': env.get('WEBUDDY_TARGET_ID'), 'verb': env.get('WEBUDDY_VERB'),
        'intent_digest': env.get('WEBUDDY_INTENT_DIGEST'), 'state': state}})
if env.get('WEBUDDY_ACTION_ID'):
    state = config.get('apply_state', 'applied')
    line = receipt(state, env['WEBUDDY_ACTION_ID'])
    with ledger.open('a') as out:
        out.write(line + '\\n')          # durable on the target, not in our reply
    if not config.get('drop_response'):
        print(config['output']); print(line)
    sys.exit(config['exit_code'])
if env.get('WEBUDDY_QUERY_ACTION_ID'):
    print(config['output'])
    asked = env['WEBUDDY_QUERY_ACTION_ID']
    for line in (ledger.read_text().splitlines() if ledger.exists() else []):
        if json.loads(line[len(marker):])['action_id'] == asked:
            print(line)
    sys.exit(config['exit_code'])
print(config['output'])
sys.exit(config['exit_code'])
'''
    for name in ('ssh', 'ssh-keyscan'):
        path = tmp_path / 'bin' / name
        path.write_text(script)
        path.chmod(0o755)
    return ledger


def test_the_write_really_delivers_its_identity_and_the_receipt_is_persisted(
        remote_env, tmp_path):
    """The forward path, end to end: the target learns the action and answers for it.

    Previously the only thing that ever put an action id near the target was the
    test writing one into the fake's canned output. Here the id reaches the target
    on the command it runs, the target files its own receipt, and what comes back
    is what settles the write.
    """
    _, store, service, _, _, target, config, calls = remote_env
    ledger = _cooperating_target(tmp_path, config)
    run = release(remote_env)
    result = service.remote.execute(run['id'], target['id'], 'deploy')
    aid = action_id(run['id'], target['id'], 'deploy')
    digest = intent_digest(intent_of(target, 'deploy'))

    written = _writes(calls)
    assert len(written) == 1
    command = json.loads(written[0])[-1]
    for assignment in (f'{ACTION_ENV}={aid}', f'{INTENT_ENV}={digest}',
                       f'{TARGET_ENV}={target["id"]}', f'{VERB_ENV}=deploy'):
        assert assignment in command, f'the write did not carry {assignment}'
    assert command.endswith(' /srv/deploy'), 'the registered command text was rewritten'

    # The target's own ledger, not our canned output, is where this came from.
    filed = json.loads(ledger.read_text().splitlines()[0][len(RECEIPT_MARKER):])
    assert filed['action_id'] == aid and filed['intent_digest'] == digest
    assert result['status'] == 'pass' and result['receipt']['state'] == 'applied'
    assert _row(store, run['id'], target['id'])['result'] == json.dumps(result)


def test_a_lost_response_is_settled_by_asking_the_target_that_filed_it(
        remote_env, tmp_path, monkeypatch):
    """The write lands, the reply is dropped, a reopened coordinator asks and settles.

    This is the shape the durable row exists for. The target performed the write
    and filed its receipt; only the answer was lost. Recovery must arrive at
    `pass` by querying, and must not send the write a second time.
    """
    client, store, service, _, _, target, config, calls = remote_env
    ledger = _cooperating_target(tmp_path, config)
    run = release(remote_env)
    data = json.loads(config.read_text())
    config.write_text(json.dumps({**data, 'drop_response': True, 'exit_code': 255}))
    aid = action_id(run['id'], target['id'], 'deploy')
    first = service.remote.execute(run['id'], target['id'], 'deploy')
    assert first['status'] == 'unverified', 'a dropped reply must not read as success'
    assert len(_writes(calls)) == 1 and ledger.exists(), 'premise: the write did land'
    client.close()

    config.write_text(json.dumps({**data, 'output': 'active (running)'}))
    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'pass'
    assert settled['reconciled'] == 'target_reported_action'
    assert settled['receipt']['action_id'] == aid
    assert len(_writes(calls)) == 1, 'reconciliation re-sent the write'
    verbs = [json.loads(line)[-1] for line in calls.read_text().splitlines()]
    assert verbs[-1] == _query_command(aid, target)


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


def _receipt(aid, target, *, verb='deploy', state='applied', **over):
    payload = {'schema': RECEIPT_SCHEMA, 'action_id': aid, 'target_id': target['id'],
               'verb': verb, 'intent_digest': intent_digest(intent_of(target, verb)),
               'state': state, **over}
    return RECEIPT_MARKER + json.dumps(payload)


def _answer(config, output):
    data = json.loads(config.read_text())
    config.write_text(json.dumps({**data, 'output': output}))


@pytest.mark.parametrize('label, state', [
    ('the target says it failed', 'failed'),
    ('the target says it does not know', 'unknown'),
])
def test_a_receipt_that_does_not_declare_applied_is_not_success(
        remote_env, tmp_path, monkeypatch, label, state):
    """Only `applied` settles. The other declared states are answers, not confirmations."""
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    client.close()
    _answer(config, 'active (running)\n' + _receipt(aid, target, state=state))

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified', label
    assert settled['reconciled'] == 'unknown_target_cannot_confirm'
    assert settled['receipt']['state'] == state
    assert not _writes(calls)


def test_two_receipts_that_disagree_leave_the_action_unknown(
        remote_env, tmp_path, monkeypatch):
    """A target contradicting itself about one action cannot be taken at its word."""
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    client.close()
    _answer(config, '\n'.join(('active (running)',
                               _receipt(aid, target, state='applied'),
                               _receipt(aid, target, state='failed'))))

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified'
    assert settled['receipt'] == {'state': 'unknown', 'conflict': True,
                                  'declared': ['applied', 'failed'], 'malformed': 0}
    assert not _writes(calls)


@pytest.mark.parametrize('label, line', [
    ('not valid json at all', RECEIPT_MARKER + '{not json'),
    ('another schema entirely', RECEIPT_MARKER + json.dumps(
        {'schema': 'other/1', 'state': 'applied'})),
])
def test_a_broken_receipt_is_a_broken_answer_not_an_absent_one(
        remote_env, tmp_path, monkeypatch, label, line):
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    client.close()
    _answer(config, 'active (running)\n' + line)

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified', label
    assert settled['receipt']['conflict'] is True and settled['receipt']['malformed'] == 1
    assert not _writes(calls)


@pytest.mark.parametrize('label, over', [
    ('claims a different verb', {'verb': 'rollback'}),
    ('claims a different target', {'target_id': 'someone-else'}),
    ('claims a different frozen intent', {'intent_digest': '0' * 32}),
    ('declares a state outside the closed list', {'state': 'probably-fine'}),
])
def test_a_receipt_bound_to_something_else_cannot_settle_this_action(
        remote_env, tmp_path, monkeypatch, label, over):
    """Right action id, wrong binding. Each of these is the same id on another claim."""
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    client.close()
    _answer(config, 'active (running)\n' + _receipt(aid, target, **over))

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified', label
    assert settled['reconciled'] == 'unknown_target_cannot_confirm'
    assert not _writes(calls)


def test_a_receipt_for_another_action_is_simply_not_ours(
        remote_env, tmp_path, monkeypatch):
    """An unrelated well-formed receipt is not a contradiction, and not a pass either."""
    client, store, service, _, _, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    client.close()
    other = action_id(run['id'], target['id'], 'rollback')
    _answer(config, 'active (running)\n' + _receipt(other, target, verb='rollback'))

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified'
    assert 'receipt' not in settled, 'another action\'s receipt was read as this one\'s'
    assert not _writes(calls)


def test_a_same_numbered_claim_from_a_reconfigured_host_is_refused(
        remote_env, tmp_path, monkeypatch):
    """The action id is derived, so a re-pointed target answers with the same number.

    Nothing about the receipt is wrong here: the schema, the id, the verb all
    match, and the target is happy to say `applied`. It is simply not the machine
    the action was claimed against any more, so the frozen configuration is
    checked before anyone is asked.
    """
    client, store, service, p, headers, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    _answer(config, 'active (running)\n' + _receipt(aid, target))

    # The admin re-points the target after the action was claimed.
    values = {k: target[k] for k in ('name', 'port', 'user', 'host_fingerprint',
                                     'commands')}
    service.targets.update(target['id'], {**values, 'host': 'elsewhere.test'},
                           target['revision'], 1)
    client.close()

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified'
    assert settled['reconciled'] == 'frozen_target_changed'
    assert 'host 已变化' in settled['reason']
    # Refused before any I/O: nothing was asked of the new host at all.
    assert not calls.exists(), 'a reconfigured host was asked to claim the same action'


def test_a_target_with_no_registered_query_command_stays_unknown(
        remote_env, tmp_path, monkeypatch):
    """No way to ask means unknown -- not a failure, and certainly not a pass."""
    client, store, service, p, headers, target, config, calls = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    commands = {k: v for k, v in target['commands'].items() if k != 'service_status'}
    values = {k: target[k] for k in ('name', 'host', 'port', 'user', 'host_fingerprint')}
    service.targets.update(target['id'], {**values, 'commands': commands},
                           target['revision'], 1)
    client.close()

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    settled = svc2.remote.reconcile(run['id'], target['id'], 'deploy')
    assert settled['status'] == 'unverified'
    assert not _writes(calls)


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
