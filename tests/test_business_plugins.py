"""Business-plugin registration and the service-side availability gate.

What these tests are actually for: a stop that only hides a button looks
identical, in a report, to a stop that works.  So every refusal below is driven
through a *real* surface -- the HTTP app, and the CLI as a separate OS process
against the same database -- rather than by calling the gate directly.  The one
test that pokes the state row by hand says in its own body why, and it is the
race case that no reachable transition can produce.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from factory.control import plugins
from factory.control.issue_maintenance_webuddy import active_task_count
from factory.control.store import Conflict
from tests.test_control_app import app_env, login, project  # noqa: F401
from tests.test_maintenance_routes import (  # noqa: F401
    _base_sha, _controlled_task, _valid_request,
)
from tests.test_issue_maintenance_cli import (  # noqa: F401
    OS_USER, _cli, _git, _request, seeded,
)

MAINT = 'issue-maintenance'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _availability(store):
    return plugins.PluginAvailability(store)


def _drain(store, actor='owner'):
    return _availability(store).set_state(MAINT, 'draining', actor=actor)


def _disable(store, actor='owner'):
    """Stop the plugin the way an administrator actually has to: drain first.

    The probe is the production one, so a test that leaves live work behind
    fails here rather than quietly disabling with tasks still running.
    """
    av = _availability(store)
    av.set_state(MAINT, 'draining', actor=actor)
    return av.set_state(MAINT, 'disabled', actor=actor,
                        active_probe=lambda: active_task_count(store))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_three_scenarios_are_declared_with_their_existing_pack_ids():
    ids = [d.id for d in plugins.declarations()]
    assert ids == ['issue-maintenance', 'legacy-modernization', 'api-adaptation']
    for decl in plugins.declarations():
        # The Skill identity is the one that already shipped; a plugin does not
        # get to rename the method pack a receipt cites.
        assert decl.skill_pack_id == decl.id
        assert decl.compatible()


def test_all_three_have_handlers_but_only_the_pre_existing_one_seeds_on():
    """Declared executable is not the same as on by default.

    All three now have a real handler, so all three may be enabled. Only
    自动化运维 seeds ``enabled``: its surface was already live before the plugin
    layer existed and seeding it off would have stopped a working feature.
    A newly wired scenario seeds off, so upgrading an existing install does not
    silently switch two new business surfaces on for every customer.
    """
    for pid in ('issue-maintenance', 'legacy-modernization', 'api-adaptation'):
        assert plugins.declaration(pid).executable is True, pid
    assert plugins.declaration(MAINT).default_enabled is True
    assert plugins.declaration('legacy-modernization').default_enabled is False
    assert plugins.declaration('api-adaptation').default_enabled is False


def test_a_newly_wired_plugin_seeds_off_and_an_admin_turns_it_on(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    av = _availability(store)
    assert av.view('api-adaptation')['state'] == 'disabled'
    assert av.view('legacy-modernization')['state'] == 'disabled'
    # Reads are reachable while off; creating is not.
    assert client.get('/api/v2/adaptation/availability',
                      headers=headers).json()['state'] == 'disabled'
    turned_on = client.post('/api/v2/plugins/api-adaptation/state',
                            json={'state': 'enabled'}, headers=headers)
    assert turned_on.status_code == 200, turned_on.text
    assert turned_on.json()['state'] == 'enabled'
    assert client.get('/api/v2/adaptation/availability',
                      headers=headers).json()['can_create'] is True
    # Turning one on leaves the others where they were.
    assert av.view('legacy-modernization')['state'] == 'disabled'
    assert av.view(MAINT)['state'] == 'enabled'


def test_unknown_plugin_id_is_refused_not_defaulted(app_env):
    _, store, _, _ = app_env
    with pytest.raises(KeyError):
        _availability(store).state('no-such-plugin')


def test_declaration_outside_the_host_contract_range_is_incompatible():
    decl = plugins.PluginDeclaration(
        id='x', name='x', version=1,
        contract_min=plugins.CONTRACT_VERSION + 1,
        contract_max=plugins.CONTRACT_VERSION + 1, skill_pack_id='x')
    assert decl.compatible() is False


# ---------------------------------------------------------------------------
# availability state machine
# ---------------------------------------------------------------------------
def test_a_plugin_with_no_handler_cannot_be_enabled(app_env, monkeypatch):
    """The rule has to stay covered even when nothing shipped is in that state.

    Every declared plugin now has a handler, so this stands one of them down to
    ``executable=False`` for the length of the test. Dropping the test instead
    would let the rule rot until the next unimplemented plugin is declared --
    and the failure mode it prevents (an entry that looks ready with nothing
    behind it) only shows up after a customer has committed to the workflow.
    """
    _, store, _, _ = app_env
    av = _availability(store)
    unwired = dataclasses.replace(plugins.declaration('api-adaptation'),
                                  executable=False)
    monkeypatch.setitem(plugins._BY_ID, 'api-adaptation', unwired)
    with pytest.raises(Conflict) as exc:
        av.set_state('api-adaptation', 'enabled', actor='owner')
    assert '尚未接入可执行处理器' in str(exc.value)
    assert av.view('api-adaptation')['state'] == 'disabled'


def test_stopping_must_go_through_draining(app_env):
    _, store, _, _ = app_env
    av = _availability(store)
    assert av.view(MAINT)['state'] == 'enabled'
    with pytest.raises(Conflict) as exc:
        av.set_state(MAINT, 'disabled', actor='owner', active_probe=lambda: 0)
    assert '排空' in str(exc.value)
    assert av.view(MAINT)['state'] == 'enabled'


def test_draining_refuses_to_stop_while_executions_are_live(app_env):
    _, store, _, _ = app_env
    av = _availability(store)
    av.set_state(MAINT, 'draining', actor='owner')
    with pytest.raises(Conflict) as exc:
        av.set_state(MAINT, 'disabled', actor='owner', active_probe=lambda: 2)
    # The count is named, and the refusal says the stop did not cancel anything.
    assert '2' in str(exc.value) and '不会自动取消' in str(exc.value)
    assert av.view(MAINT)['state'] == 'draining'
    assert av.set_state(MAINT, 'disabled', actor='owner',
                        active_probe=lambda: 0)['state'] == 'disabled'


def test_second_administrator_cannot_silently_overwrite_a_decision(app_env):
    _, store, _, _ = app_env
    av = _availability(store)
    stale = av.view(MAINT)['revision']
    av.set_state(MAINT, 'draining', actor='first')
    with pytest.raises(Conflict):
        av.set_state(MAINT, 'enabled', actor='second', expected_revision=stale)
    assert av.view(MAINT)['state'] == 'draining'


def test_every_transition_is_audited_and_the_audit_cannot_be_rewritten(app_env):
    _, store, _, _ = app_env
    av = _availability(store)
    av.set_state(MAINT, 'draining', actor='owner')
    entries = av.audit(MAINT)
    assert entries[0]['actor'] == 'owner'
    assert entries[0]['data'] == {'from': 'enabled', 'to': 'draining'}
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE plugin_availability_audit SET actor='someone-else'")


def test_active_count_reads_the_engine_states_not_a_counter(app_env):
    """Live/finished is decided by the run's own state, not by a tally we keep.

    The same task is walked through three engine states; a counter maintained
    on the side would have to be told about each move, and would be wrong the
    first time a run changed state without going through this module.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    assert active_task_count(store) == 0
    task_id, p, run_id, _ = _controlled_task(client, store, repo, headers, 'running')
    assert active_task_count(store) == 1
    store.update(run_id, {'status': 'ready_for_review'})
    assert active_task_count(store) == 0
    store.update(run_id, {'status': 'needs_human'})
    assert active_task_count(store) == 1


# ---------------------------------------------------------------------------
# HTTP surface: the states an administrator can actually reach
# ---------------------------------------------------------------------------
def test_enabled_plugin_creates_tasks_and_the_list_says_so(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text
    listed = client.get(f'/api/v2/maintenance/tasks?project_id={p["id"]}',
                        headers=headers).json()
    assert listed['availability']['state'] == 'enabled'
    assert listed['availability']['can_create'] is True
    assert len(listed['tasks']) == 1


def test_draining_refuses_new_tasks_over_http(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    _drain(store)
    refused = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert refused.status_code == 409, refused.text
    assert '排空' in refused.json()['detail']
    listed = client.get(f'/api/v2/maintenance/tasks?project_id={p["id"]}',
                        headers=headers).json()
    assert listed['availability']['can_create'] is False
    assert listed['tasks'] == []


def test_disabled_plugin_refuses_new_tasks_over_http(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    _disable(store)
    refused = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert refused.status_code == 409, refused.text
    assert '已停用' in refused.json()['detail']


def test_history_receipt_and_patch_survive_a_stop(app_env):
    """Stopping the plugin must not make finished customer work unreachable."""
    client, store, svc, repo = app_env
    headers = login(client)
    sha = _base_sha(repo)
    subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'fix'],
                   cwd=repo, check=True)
    head = _base_sha(repo)
    task_id, p, run_id, _ = _controlled_task(
        client, store, repo, headers, 'ready_for_review',
        artifacts={'commit': head, 'base_sha': sha, 'worktree': str(repo),
                   'checks': [{'name': 'local', 'exit': 0}], 'unverified': []},
        base_sha=sha)

    _disable(store)

    detail = client.get(f'/api/v2/maintenance/tasks/{task_id}', headers=headers)
    assert detail.status_code == 200
    assert detail.json()['status'] == 'delivered'
    assert client.get(f'/api/v2/maintenance/tasks/{task_id}/events',
                      headers=headers).status_code == 200
    exported = client.get(f'/api/v2/maintenance/tasks/{task_id}/export',
                          headers=headers)
    assert exported.status_code == 200, exported.text
    name = exported.json()['artifacts'][0]['name']
    patch = client.get(
        f'/api/v2/maintenance/tasks/{task_id}/artifacts/{name}', headers=headers)
    assert patch.status_code == 200 and patch.content


def test_draining_still_lets_live_work_be_supplemented_approved_and_cancelled(app_env):
    """The whole point of draining: intake stops, work in flight does not."""
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, p, run_id, _ = _controlled_task(client, store, repo, headers, 'needs_human')
    _drain(store)

    follow = client.post(f'/api/v2/maintenance/tasks/{task_id}/follow-up',
                         json={'content': '补充一条复现条件'}, headers=headers)
    assert follow.status_code == 200, follow.text
    cancelled = client.post(f'/api/v2/maintenance/tasks/{task_id}/cancel',
                            headers=headers)
    assert cancelled.status_code == 200, cancelled.text


def test_approval_is_checked_too_even_though_it_bypasses_the_port(app_env):
    """``approve`` hands its decision to the run lifecycle, not to the port.

    No reachable transition produces "disabled while a task awaits approval" --
    draining refuses to stop while that task is counted as live. What remains is
    the window between counting and writing the state row, so the row is written
    directly here to stand in for that race. Without the hand-written check in
    ``maintenance_routes.approve`` this returns 200 and the executor starts
    changing files under a stopped plugin.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, p, run_id, _ = _controlled_task(client, store, repo, headers, 'needs_human')
    store.update(run_id, {'status': 'awaiting_approval',
                          'plan': {'title': '修复导出', 'tasks': []}})
    with store.connect() as db:
        db.execute("UPDATE plugin_availability SET state='disabled' WHERE plugin_id=?",
                   (MAINT,))
    refused = client.post(f'/api/v2/maintenance/tasks/{task_id}/approve', headers=headers)
    assert refused.status_code == 409, refused.text
    assert '已停用' in refused.json()['detail']
    assert store.get(run_id)['status'] == 'awaiting_approval'


def test_stopping_one_plugin_does_not_stop_another(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    # legacy-modernization ships disabled; maintenance must not notice.
    assert _availability(store).view('legacy-modernization')['state'] == 'disabled'
    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text


# ---------------------------------------------------------------------------
# HTTP surface: who may change availability
# ---------------------------------------------------------------------------
def test_admin_can_read_and_move_availability_over_http(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    listing = client.get('/api/v2/plugins', headers=headers).json()
    assert listing['contract_version'] == plugins.CONTRACT_VERSION
    assert {p['id'] for p in listing['plugins']} == {
        'issue-maintenance', 'legacy-modernization', 'api-adaptation'}
    moved = client.post(f'/api/v2/plugins/{MAINT}/state',
                        json={'state': 'draining'}, headers=headers)
    assert moved.status_code == 200, moved.text
    assert moved.json()['state'] == 'draining'
    assert client.get(f'/api/v2/plugins/{MAINT}/audit',
                      headers=headers).json()['entries'][0]['data']['to'] == 'draining'
    bad = client.post(f'/api/v2/plugins/{MAINT}/state',
                      json={'state': 'paused'}, headers=headers)
    assert bad.status_code == 422, bad.text
    assert client.post('/api/v2/plugins/no-such/state',
                       json={'state': 'draining'}, headers=headers).status_code == 404


def test_member_cannot_change_plugin_availability(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    client.app.state.auth.create_user('plugin-member', 'member-long-password',
                                      role='member')
    response = client.post('/api/auth/login',
                           json={'username': 'plugin-member',
                                 'password': 'member-long-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200
    member = {'Origin': 'http://testserver',
              'X-CSRF-Token': response.json()['csrf_token']}
    assert client.get('/api/v2/plugins', headers=member).status_code == 200
    refused = client.post(f'/api/v2/plugins/{MAINT}/state',
                          json={'state': 'draining'}, headers=member)
    assert refused.status_code == 403, refused.text
    assert _availability(store).view(MAINT)['state'] == 'enabled'


# ---------------------------------------------------------------------------
# CLI: a different process, the same stop
# ---------------------------------------------------------------------------
def test_cli_create_is_refused_after_the_plugin_is_stopped(seeded):
    """The CLI is a separate OS process, so this is not the web gate re-tested.

    Before the CLI was rewired onto ``tasks_for``, it assembled its own port:
    an administrator could stop the plugin in the web UI and this command would
    still dispatch a paid execution.
    """
    _disable(seeded.store)
    req = _request(seeded.base_sha, project_id=seeded.project['id'],
                   idempotency_key='cli-stopped-1')
    req_file = Path(seeded.db).parent / 'stopped-request.json'
    req_file.write_text(json.dumps(req, ensure_ascii=False))
    rc, data = _cli('--db', seeded.db, '--operator', OS_USER,
                    'create', '--request-json', str(req_file), expect_ok=False)
    assert rc != 0
    assert '已停用' in json.dumps(data, ensure_ascii=False)


def test_cli_can_still_read_and_export_after_the_plugin_is_stopped(seeded):
    _disable(seeded.store)
    rc, listed = _cli('--db', seeded.db, '--operator', OS_USER,
                      'list', '--project', seeded.project['id'])
    assert rc == 0 and listed['tasks']
    rc, shown = _cli('--db', seeded.db, '--operator', OS_USER,
                     'show', seeded.view['task_id'])
    assert rc == 0 and shown['task_id'] == seeded.view['task_id']


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def test_an_unclassified_port_method_is_not_reachable_through_the_gate(app_env):
    """A business method added without an availability class must not slip past.

    ``GatedPort`` exposes only what ``MAINTENANCE_ACTIONS`` classifies, so the
    mistake surfaces as an AttributeError at the call site instead of as an
    ungated action nobody notices.
    """
    from factory.control.issue_maintenance_webuddy import tasks_for
    _, store, svc, _ = app_env
    tasks = tasks_for(svc)
    assert set(plugins.MAINTENANCE_ACTIONS) <= set(dir(tasks.port))
    with pytest.raises(AttributeError):
        tasks.records  # real attribute of the underlying port, deliberately hidden
