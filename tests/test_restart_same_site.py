"""A restart must resume the same site, proven across a genuinely reopened database.

Every earlier restart test called `recover()` on the service instance that had just
written the checkpoint, so the objects under test were still the ones holding the
state in memory. That cannot distinguish "the site was durably recorded" from "the
site is still in this process". These tests close the first service, construct a
second `Store` and `Service` over the same `control.db` file, and only then recover.
"""
from __future__ import annotations

import json
import subprocess
import threading

import pytest

from factory.control import effective_contract as ec
from factory.control.autonomy import DEFAULT_POLICY
from factory.control.service import Service
from factory.control.store import Conflict, Store
from tests.test_control_app import FakeSDK, login, project
from tests.test_workbench_app import app_env


def _confirmed_continuous(client, store, svc, repo, headers):
    """A continuous run with a signed specification, stopped mid-execution."""
    p = project(client, repo, headers)
    current = svc.policies.get(p['id'])
    svc.policies.update(p['id'], {**DEFAULT_POLICY, 'resume_on_restart': True},
                        current['revision'], 'owner')
    rid = store.create_run(p['id'], '做一个计算工具，只加 double',
                           source={'type': 'web', 'actor': 'owner', 'actor_id': 1,
                                   'original_request': '做一个计算工具，只加 double'})[0]['id']
    draft = {'goal': '提供 double 计算', 'screens': [], 'flows': ['输入数字得到 double'],
             'data_model': ['number'], 'non_goals': ['不做 square'], 'risks_assumptions': []}
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()
    checkpoint = {'execution_mode': 'continuous', 'base_sha': base,
                  'worktree': p['workspace'], 'branch': f'factory/{rid}',
                  'session_id': 'durable-session', 'commit': None,
                  'execution_checks': store.project(p['id'])['checks'],
                  'known_cost_usd': 0.0, 'observed_cost_usd': 0.0,
                  'tasks': [{'id': 'coding', 'status': 'running'}]}
    store.update(rid, {'status': 'running', 'execution_mode': 'continuous',
                       'spec_draft': draft,
                       'spec_confirmation': {'actor': 'owner', 'at': 'now'},
                       'requirement_spec_path': 'docs/spec.md',
                       'execution_checks': store.project(p['id'])['checks'],
                       'plan': {'title': 'double', 'summary': '', 'questions': [],
                                'tasks': [{'id': 'coding', 'title': 'a', 'prompt': 'double',
                                           'acceptance': ['ok'], 'paths': ['x'],
                                           'checks': ['greeting'], 'depends_on': [],
                                           'complexity': 'small', 'risk': 'low'}]}})
    store.append(rid, 'execution.checkpoint',
                 {'execution_mode': 'continuous', 'continuous_artifacts': checkpoint})
    return p, rid, checkpoint


def _reopened(tmp_path, monkeypatch):
    """A second coordinator over the same durable file, as a real restart is."""
    store = Store(tmp_path / 'data' / 'control.db')
    svc = Service(store, runner=FakeSDK(),
                  profiles={role: {'provider': 'codex', 'model': 'test'}
                            for role in ('planner', 'cheap', 'standard', 'strong')})
    monkeypatch.setattr(svc, '_ensure_scheduler', lambda: None)
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: None)
    return store, svc


def test_restart_binds_the_same_agreement_and_keeps_the_accounting(app_env, monkeypatch,
                                                                  tmp_path):
    """The recovered round carries the agreement it stopped under, by digest.

    `recovery.recover` built an `execution_resume` with no agreement binding at
    all, so `_run` fell back to whatever `current(run)` produced when the restarted
    round reached the coding call. The revision number alone would not have caught
    it either: this run's agreement is still derived from `spec_draft`, so an edit
    to that draft keeps revision 1 and changes the snapshot underneath it.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p, rid, checkpoint = _confirmed_continuous(client, store, svc, repo, headers)
    original = ec.current(store.get(rid))
    # An interrupted coding call whose cost was never reconciled.
    store.append(rid, 'provider.started', {'profile': 'standard', 'provider': 'codex',
                                           'model': 'test', 'call_id': 'call-1',
                                           'max_budget_usd': 4.0}, 'coding')
    svc.queue.release()

    store2, svc2 = _reopened(tmp_path, monkeypatch)
    svc2.recover()

    resumed = store2.get(rid)
    resume = resumed['execution_resume']
    assert resumed['status'] == 'queued'
    assert resume['artifacts'] == checkpoint
    assert resume['effective_revision'] == original['revision'] == 1
    assert resume['effective_digest'] == original['digest']
    assert resume['effective_source'] == 'spec_draft'
    # Same site: the checks the round was dispatched with, and the retry counter.
    assert resumed['execution_checks'] == store2.project(p['id'])['checks']
    assert resumed['resume_count'] == 1
    # The interrupted call is still charged across the restart: an unreconciled
    # call is held as unknown cost, not silently reset to free.
    usage = svc2._usage(rid)
    assert usage['unknown_cost_calls'] == 1
    ledger = svc2._budget_usage(rid, store2.project(p['id']))
    assert ledger['unknown_cost_reserved_usd'] == 4.0
    assert ledger['effective_cost_usd'] == 4.0
    # And the bound agreement is enforced, not merely recorded: if the confirmed
    # draft is edited before the recovered round starts, the round refuses instead
    # of coding against an agreement nobody bound it to.
    moved = {**resumed, 'spec_draft': {**resumed['spec_draft'], 'goal': '提供 square 计算'}}
    with pytest.raises(Conflict) as raised:
        ec.contract_prompt(moved, revision=resume['effective_revision'],
                           digest=resume['effective_digest'])
    assert raised.value.error_type == 'contract_revision'
    assert ec.revision_of(moved) == resume['effective_revision']  # the number did not move
