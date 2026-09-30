"""Synthetic, quiesced SQLite drill: business state + access boundaries survive.

This does not restore production data or claim that worktrees/artifacts/keys are
inside the SQLite-only snapshot. No provider or external publish call is made.
"""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from factory.control.auth import AuthError, AuthStore
from factory.control.governance import Governance
from factory.control.store import Store


spec = importlib.util.spec_from_file_location('snapshot_for_drill',
    Path(__file__).parents[1] / 'docs/handbook/tools/sqlite_snapshot.py')
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def test_quiesced_snapshot_restores_business_state_and_scope(tmp_path):
    source = tmp_path / 'live-synthetic'
    auth = AuthStore(source / 'users.db')
    store = Store(source / 'control.db')
    governance = Governance(auth, store)
    owner = auth.create_user('owner', 'synthetic-owner-password')
    member = auth.create_user('member', 'synthetic-member-password', role='member')
    first = store.add_project({'name': 'Allowed', 'repository': 'synthetic/allowed',
                               'workspace': str(tmp_path / 'workspace-a'), 'checks': {}})
    second = store.add_project({'name': 'Other', 'repository': 'synthetic/other',
                                'workspace': str(tmp_path / 'workspace-b'), 'checks': {}})
    governance.assign(member['id'], [first['id']], 'owner')
    run, _ = store.create_run(first['id'], 'Synthetic restore drill',
                              source={'type': 'web', 'actor_id': member['id']})
    store.update(run['id'], {'status': 'ready_for_review'})
    store.append(run['id'], 'synthetic.evidence', {'result': 'verified-local-fixture',
                 'DEPLOY_PASSWORD': 'synthetic-never-archive'})
    previous_events = store.events(run['id'])
    old_token, _, _ = auth.login('member', 'synthetic-member-password')
    # Store/AuthStore use short-lived connections. No background service starts.
    before = {name: snapshot.digest(source / name) for name in snapshot.DATABASES}
    backup = tmp_path / 'snapshot'
    snapshot.snapshot(source, backup)
    snapshot.verify(backup)
    assert before == {name: snapshot.digest(source / name) for name in snapshot.DATABASES}
    restored = tmp_path / 'isolated-restore'
    shutil.copytree(backup, restored)
    restored_auth = AuthStore(restored / 'users.db')
    restored_store = Store(restored / 'control.db')
    restored_governance = Governance(restored_auth, restored_store)
    _, _, restored_member = restored_auth.login('member', 'synthetic-member-password')
    assert restored_member['id'] == member['id']
    assert restored_member['role'] == 'member'
    assert len(restored_store.projects()) == 2
    assert restored_store.get(run['id'])['status'] == 'ready_for_review'
    assert restored_store.events(run['id']) == previous_events
    assert 'synthetic-never-archive' not in json.dumps(previous_events)
    restored_governance.require_read_project(restored_member, first['id'])
    with pytest.raises(AuthError):
        restored_governance.require_read_project(restored_member, second['id'])
    # Restoring users.db also restores sessions. Document, detect and explicitly
    # revoke the synthetic old session; never label the snapshot credential-free.
    assert restored_auth.authenticate(old_token) is not None
    restored_auth.logout(old_token)
    assert restored_auth.authenticate(old_token) is None
    snapshot.verify(backup)  # The original backup was never opened for writes.
    assert not (backup / 'workspace-a').exists()
    assert owner['role'] == 'admin'
