"""Durable feedback adoption, safe boundaries and predecessor isolation."""
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from factory.control.agents import AgentStore
from factory.control.service import Service
from factory.control.store import Store


STABLE_CLAUDE_ORIGIN = {
    'provider': 'claude', 'schema': 'stable_dynamic_sections_v1'}


def mark_claude_session_configuration(store, rid, model, *, strategy='fresh'):
    payload = {
        'provider': 'claude', 'model': model, 'read_only': False,
        'session_strategy': strategy,
    }
    if strategy == 'fresh':
        payload['session_origin'] = dict(STABLE_CLAUDE_ORIGIN)
    store.append(rid, 'provider.configuration', payload, 'coding')


def setup(tmp_path, status='running'):
    store = Store(tmp_path / 'state.db')
    agents = AgentStore(store)
    aid = agents.create({'name': 'Maintainer'}, '1')['id']
    c = agents.create_conversation(aid, 'do', 'project', 1)
    run, _ = store.create_run('project', 'Build a tool', source={'type': 'agent', 'actor_id': 1})
    store.update(run['id'], {'status': status, 'conversation_id': c['id'], 'agent_id': aid,
        'agent_version': 1, 'agent_snapshot': agents.version(aid),
        'runtime_configuration': {'revision': 7}})
    agents.attach_run(c['id'], run['id'])
    agents.append_message(c['id'], 'user', 'Keep the new output and add CSV', feedback_status='pending')
    return store, agents, c['id'], run['id']


def test_concurrent_adoption_creates_one_successor_with_frozen_identity(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    agents.append_message(cid, 'user', 'Include a header', feedback_status='pending')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: agents.adopt_feedback(cid), range(8)))
    successor = next(r for r in results if r)
    assert sum(r is not None for r in results) == 1
    assert len(store.all_runs()) == 2
    assert successor['source']['actor_id'] == 1
    assert successor['agent_snapshot'] == store.get(rid)['agent_snapshot']
    assert successor['runtime_configuration'] == {'revision': 7}
    assert 'Include a header' in successor['history'][-1]
    assert agents.conversation(cid)['pending_feedback_count'] == 0
    assert all(m['feedback_run_id'] == successor['id'] for m in agents.conversation(cid)['messages'] if m['role'] == 'user')


def test_feedback_successor_carries_only_verified_continuous_session_seed(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': '  prior-session  ',
        'session_profile': {
            'profile': 'strong', 'provider': 'claude', 'model': 'claude-opus-5',
            'reason': 'previous routing decision',
        },
        'checks': [{'name': 'tests', 'exit': 0}],
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    mark_claude_session_configuration(store, rid, 'claude-opus-5')

    successor = agents.adopt_feedback(cid)

    assert successor['artifacts'] == {}
    assert successor['feedback_session'] == {
        'session_id': 'prior-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-opus-5'},
        'previous_run_id': rid,
        'session_origin': {
            **STABLE_CLAUDE_ORIGIN,
            'origin_run_id': rid,
            'origin_session_id': 'prior-session',
        },
    }
    assert successor['feedback_session_strategy']['strategy'] == 'resume'
    event = next(item for item in store.events(successor['id'])
                 if item['type'] == 'feedback.adopted')
    assert event['payload']['session_continuation_available'] is True
    assert event['payload']['session_strategy']['reason'] == 'verified_session_available'
    assert 'checks' not in successor['feedback_session']
    assert 'worktree' not in successor['feedback_session']


def test_predeployment_claude_session_without_origin_marker_cold_starts_once(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'predeployment-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    # A current client can report that it resumed with the new settings, but
    # that does not prove which prompt schema created this old remote session.
    mark_claude_session_configuration(
        store, rid, 'claude-sonnet-5', strategy='resume')
    store.append(rid, 'usage.recorded', {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'input_tokens': 2_000_000, 'output_tokens': 20_000,
        'cached_input_tokens': 1_500_000,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 3.0,
    }, 'coding')

    successor = agents.adopt_feedback(cid)

    assert 'feedback_session' not in successor
    assert successor['feedback_session_strategy'] == {
        'strategy': 'cold_start',
        'reason': 'legacy_claude_session_origin_unknown',
        'previous_run_id': rid,
        'required_origin_schema': 'stable_dynamic_sections_v1',
    }


def test_stable_claude_origin_survives_multiple_feedback_resumes(tmp_path):
    store, agents, cid, first_id = setup(tmp_path, 'ready_for_review')
    store.update(first_id, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/first',
        'branch': 'factory/first',
        'commit': 'a' * 40,
        'session_id': 'origin-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
    }})
    mark_claude_session_configuration(store, first_id, 'claude-sonnet-5')
    second = agents.adopt_feedback(cid)
    origin = {
        **STABLE_CLAUDE_ORIGIN,
        'origin_run_id': first_id,
        'origin_session_id': 'origin-session',
    }
    assert second['feedback_session']['session_origin'] == origin

    # The SDK is allowed to return a derived session ID after resume. The
    # origin marker remains attached to the durable lineage, not that ID.
    store.update(second['id'], {
        'status': 'ready_for_review',
        'artifacts': {
            'execution_mode': 'continuous',
            'worktree': '/project/worktrees/second',
            'branch': 'factory/second',
            'commit': 'b' * 40,
            'session_id': 'derived-session',
            'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        },
    })
    mark_claude_session_configuration(
        store, second['id'], 'claude-sonnet-5', strategy='resume')
    agents.append_message(
        cid, 'user', 'Apply one more narrow change', feedback_status='pending')

    third = agents.adopt_feedback(cid)

    assert third['feedback_session']['session_id'] == 'derived-session'
    assert third['feedback_session']['session_origin'] == origin
    assert third['feedback_session_strategy']['strategy'] == 'resume'


def test_feedback_successor_does_not_seed_incomplete_provider_session(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'prior-session',
        'session_profile': {'provider': 'claude'},
    }})

    successor = agents.adopt_feedback(cid)

    assert 'feedback_session' not in successor
    assert successor['feedback_session_strategy'] == {
        'strategy': 'unavailable', 'reason': 'verified_session_unavailable'}
    event = next(item for item in store.events(successor['id'])
                 if item['type'] == 'feedback.adopted')
    assert event['payload']['session_continuation_available'] is False


def test_large_uncached_claude_session_cold_starts_from_verified_project_state(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'large-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    store.append(rid, 'usage.recorded', {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'input_tokens': 2_856_526, 'output_tokens': 449_742,
        'cached_input_tokens': 0, 'cache_creation_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 10.21,
    }, 'coding')
    mark_claude_session_configuration(store, rid, 'claude-sonnet-5')

    successor = agents.adopt_feedback(cid)

    assert 'feedback_session' not in successor
    assert successor['feedback_session_strategy'] == {
        'strategy': 'cold_start',
        'reason': 'large_uncached_claude_session',
        'previous_run_id': rid,
        'input_tokens': 2_856_526,
        'cached_input_tokens': 0,
        'input_limit': 500_000,
    }
    # Project truth and the current owner request still cross the boundary.
    assert successor['feedback_predecessor_id'] == rid
    assert 'Keep the new output and add CSV' in successor['request']
    assert 'Build a tool' in successor['history']
    event = next(item for item in store.events(successor['id'])
                 if item['type'] == 'feedback.adopted')
    assert event['payload']['session_continuation_available'] is False
    assert event['payload']['session_strategy']['strategy'] == 'cold_start'


def test_large_claude_session_with_real_cache_hits_remains_resumable(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'cached-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    store.append(rid, 'usage.recorded', {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'input_tokens': 2_000_000, 'output_tokens': 20_000,
        'cached_input_tokens': 1_500_000, 'cache_creation_input_tokens': 40_000,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 3.0,
    }, 'coding')
    mark_claude_session_configuration(store, rid, 'claude-sonnet-5')

    successor = agents.adopt_feedback(cid)

    assert successor['feedback_session']['session_id'] == 'cached-session'
    assert successor['feedback_session_strategy']['strategy'] == 'resume'
    assert successor['feedback_session_strategy']['cached_input_tokens'] == 1_500_000


def test_planner_usage_never_inflates_coding_session_cache_decision(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'short-coding-session',
        'session_profile': {'provider': 'claude', 'model': 'shared-model'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    common = {
        'provider': 'claude', 'model': 'shared-model',
        'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1',
    }
    store.append(rid, 'usage.recorded', {
        **common, 'profile': 'planner', 'input_tokens': 3_000_000,
        'output_tokens': 50_000, 'cost_usd': 10.0,
    }, 'planner')
    store.append(rid, 'usage.recorded', {
        **common, 'profile': 'standard', 'input_tokens': 100_000,
        'output_tokens': 5_000, 'cost_usd': 1.0,
    }, 'coding')
    mark_claude_session_configuration(store, rid, 'shared-model')

    successor = agents.adopt_feedback(cid)

    assert successor['feedback_session']['session_id'] == 'short-coding-session'
    assert successor['feedback_session_strategy']['strategy'] == 'resume'
    assert successor['feedback_session_strategy']['input_tokens'] == 100_000


def test_same_call_usage_duplicate_does_not_double_session_input(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'deduplicated-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    usage = {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'call_id': 'same-call', 'input_tokens': 300_000, 'output_tokens': 10_000,
        'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 1.0,
    }
    store.append(rid, 'usage.recorded', usage, 'coding')
    store.append(rid, 'usage.recorded', usage, 'coding')
    mark_claude_session_configuration(store, rid, 'claude-sonnet-5')

    successor = agents.adopt_feedback(cid)

    assert successor['feedback_session']['session_id'] == 'deduplicated-session'
    assert successor['feedback_session_strategy']['strategy'] == 'resume'
    assert successor['feedback_session_strategy']['input_tokens'] == 300_000


def test_same_call_unknown_usage_can_be_reconciled_to_known_usage(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'reconciled-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    identity = {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'call_id': 'reconciled-call',
    }
    store.append(rid, 'usage.recorded', {
        **identity, 'input_tokens': None, 'cached_input_tokens': None,
        'cost_usd': None, 'max_budget_usd': 3.0, 'interrupted': True,
    }, 'coding')
    store.append(rid, 'usage.recorded', {
        **identity, 'input_tokens': 600_000, 'output_tokens': 20_000,
        'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 2.0,
        'reconciled': True,
    }, 'coding')
    mark_claude_session_configuration(store, rid, 'claude-sonnet-5')

    successor = agents.adopt_feedback(cid)

    assert 'feedback_session' not in successor
    assert successor['feedback_session_strategy']['strategy'] == 'cold_start'
    assert successor['feedback_session_strategy']['input_tokens'] == 600_000


@pytest.mark.parametrize('later_payload', [
    {
        'input_tokens': 600_000, 'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1',
    },
    {'input_tokens': None, 'cached_input_tokens': None},
])
def test_same_call_conflict_or_later_unknown_is_conservative(tmp_path, later_payload):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'ambiguous-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    identity = {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'call_id': 'ambiguous-call',
    }
    store.append(rid, 'usage.recorded', {
        **identity, 'input_tokens': 550_000, 'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1',
    }, 'coding')
    store.append(rid, 'usage.recorded', {
        **identity, **later_payload,
    }, 'coding')
    mark_claude_session_configuration(store, rid, 'claude-sonnet-5')

    successor = agents.adopt_feedback(cid)

    assert successor['feedback_session']['session_id'] == 'ambiguous-session'
    assert successor['feedback_session_strategy'] == {
        'strategy': 'resume', 'reason': 'cache_telemetry_untrusted'}


def test_legacy_cache_schema_never_forces_session_cold_start(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'legacy-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-opus-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    store.append(rid, 'usage.recorded', {
        'profile': 'strong', 'provider': 'claude', 'model': 'claude-opus-5',
        'input_tokens': 600_000, 'output_tokens': 50_000,
        'cached_input_tokens': 0,
        'cache_usage_schema': 'legacy_combined_v0', 'cost_usd': 4.0,
    }, 'coding')
    store.append(rid, 'usage.recorded', {
        'profile': 'strong', 'provider': 'claude', 'model': 'claude-opus-5',
        'input_tokens': 2_400_000, 'output_tokens': 50_000,
        'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 8.0,
    }, 'coding')
    mark_claude_session_configuration(store, rid, 'claude-opus-5')

    successor = agents.adopt_feedback(cid)

    assert successor['feedback_session']['session_id'] == 'legacy-session'
    assert successor['feedback_session_strategy']['strategy'] == 'resume'
    assert successor['feedback_session_strategy']['reason'] == 'cache_telemetry_untrusted'


def test_legacy_usage_without_task_identity_conservatively_resumes(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    store.update(rid, {'artifacts': {
        'execution_mode': 'continuous',
        'worktree': '/project/worktrees/previous',
        'branch': 'factory/previous',
        'commit': 'a' * 40,
        'session_id': 'legacy-taskless-session',
        'session_profile': {'provider': 'claude', 'model': 'claude-sonnet-5'},
        'tasks': [{'id': 'coding', 'status': 'verified'}],
    }})
    store.append(rid, 'usage.recorded', {
        'profile': 'standard', 'provider': 'claude', 'model': 'claude-sonnet-5',
        'input_tokens': 2_000_000, 'output_tokens': 20_000,
        'cached_input_tokens': 0,
        'cache_usage_schema': 'separate_read_write_v1', 'cost_usd': 8.0,
    })
    mark_claude_session_configuration(store, rid, 'claude-sonnet-5')

    successor = agents.adopt_feedback(cid)

    assert successor['feedback_session']['session_id'] == 'legacy-taskless-session'
    assert successor['feedback_session_strategy'] == {
        'strategy': 'resume', 'reason': 'cache_telemetry_untrusted'}


@pytest.mark.parametrize('status', ['running', 'verifying', 'publishing', 'awaiting_approval',
    'needs_clarification', 'needs_human', 'cancelled', 'failed'])
def test_does_not_bypass_active_or_blocked_run(tmp_path, status):
    store, agents, cid, _ = setup(tmp_path, status)
    assert agents.adopt_feedback(cid) is None
    assert agents.conversation(cid)['pending_feedback_count'] == 1
    assert len(store.all_runs()) == 1


def test_drain_waits_for_worker_then_recovers_committed_unqueued_successor(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    svc = Service(store, runner=object())
    try:
        svc.active_jobs[rid] = 'execute'
        svc._drain_feedback()
        assert len(store.all_runs()) == 1
        svc.active_jobs.clear()
        # Simulate crash after durable adoption but before durable enqueue.
        successor = agents.adopt_feedback(cid)
    finally:
        svc.close()
    restored = Service(Store(tmp_path / 'state.db'), runner=object())
    try:
        # Actual startup recovery must not classify an unstarted outbox run
        # as an ambiguous write, even when its policy cannot be loaded.
        restored._ensure_scheduler = lambda: None
        restored.recover()
        restored._drain_feedback()
        restored._drain_feedback()
        assert restored.queue.pending() == [{'run_id': successor['id'], 'phase': 'plan'}]
        assert len(restored.store.all_runs()) == 2
    finally:
        restored.close()


def test_revoked_actor_leaves_feedback_pending(tmp_path):
    store, agents, cid, _ = setup(tmp_path, 'ready_for_review')
    svc = Service(store, runner=object())
    class Denied:
        def require_project(self, actor, project):
            assert (actor, project) == (1, 'project')
            raise ValueError('permission revoked')
    svc.governance = Denied()
    try:
        svc._drain_feedback()
        assert len(store.all_runs()) == 1
        assert agents.conversation(cid)['pending_feedback_count'] == 1
        assert agents.conversation(cid)['feedback_error'] == 'permission revoked'
    finally:
        svc.close()


def test_successor_context_uses_verified_parent_not_main(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.com')
    (repo / 'result.txt').write_text('original')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    original = git(repo, 'rev-parse', 'HEAD')
    checkout = tmp_path / 'verified'
    git(repo, 'worktree', 'add', '-b', 'factory/previous', str(checkout), 'main')
    (checkout / 'result.txt').write_text('verified output')
    git(checkout, 'commit', '-qam', 'result')
    commit = git(checkout, 'rev-parse', 'HEAD')
    project_id = store.add_project({'name': 'sample', 'workspace': str(repo), 'base_branch': 'main'})['id']
    store.update(rid, {'project_id': project_id})
    store.update(rid, {'artifacts': {'worktree': str(checkout), 'branch': 'factory/previous', 'commit': commit}})
    successor = agents.adopt_feedback(cid)
    svc = Service(store, runner=object())
    try:
        project = svc._project_for_run(successor)
        from factory.control.context import verify_planning_checkout
        verify_planning_checkout(project, commit)
        assert project['workspace'] == str(checkout)
        assert git(repo, 'rev-parse', 'main') == original
        assert store.project(project_id)['base_branch'] == 'main'
        # Execute the adopted request against the real verified worktree.
        import sys
        import threading
        from pathlib import Path
        from factory.control.execution import execute_plan
        from factory.control.providers import ProviderResult
        class Writer:
            def run(self, request, emit, cancel=None):
                assert (Path(request.workspace) / 'result.txt').read_text() == 'verified output'
                (Path(request.workspace) / 'second.txt').write_text('feedback result')
                return ProviderResult('done', cost_usd=0.01)
        checks = {'result': [sys.executable, '-c',
            "from pathlib import Path; assert Path('result.txt').read_text() == 'verified output'; assert Path('second.txt').read_text() == 'feedback result'"]}
        artifacts = execute_plan(run_id=successor['id'], project={**project, 'checks': checks,
            'budget_usd': 1, 'unknown_cost_policy': 'allow_bounded'},
            plan={'tasks': [{'id': 'add', 'prompt': 'add second.txt', 'paths': ['second.txt'],
                'checks': ['result'], 'acceptance': ['first result preserved'], 'complexity': 'small', 'risk': 'low'}]},
            profiles={role: {'provider': 'codex', 'model': 'test'} for role in ('cheap', 'standard', 'strong')},
            runner=Writer(), emit=lambda *args: None, cancel=threading.Event(), timeout_s=30)
        assert artifacts['base_sha'] == commit
        assert git(repo, 'rev-parse', 'main') == original
        store.update(successor['id'], {'status': 'ready_for_review', 'artifacts': artifacts})
        agents.append_message(cid, 'user', 'next change', feedback_status='pending')
        third = agents.adopt_feedback(cid)
        assert agents.adopt_feedback(cid) is None
        third_project = svc._project_for_run(third)
        assert (Path(third_project['workspace']) / 'result.txt').read_text() == 'verified output'
        assert (Path(third_project['workspace']) / 'second.txt').read_text() == 'feedback result'
        git(checkout, 'commit', '--allow-empty', '-qm', 'unexpected change')
        with pytest.raises(ValueError, match='分支已变化'):
            svc._project_for_run(successor)
    finally:
        svc.close()


from tests.test_workbench_app import app_env
from tests.test_control_app import login, project as create_project


def test_service_forwards_feedback_session_without_predecessor_artifacts(app_env, monkeypatch):
    import threading

    client, store, svc, repo = app_env
    p = create_project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'Apply only the current feedback',
        source={'type': 'agent', 'actor_id': 1})
    seed = {
        'session_id': 'prior-session',
        'session_profile': {'provider': 'claude', 'model': 'model'},
        'previous_run_id': 'verified-parent',
    }
    task = {'id': 'coding', 'title': 'Apply feedback', 'prompt': 'full task context',
        'acceptance': ['feedback works'], 'paths': ['src'],
        'checks': list(p['checks']), 'depends_on': [], 'complexity': 'small', 'risk': 'low'}
    store.update(run['id'], {
        'status': 'queued', 'revision': 1, 'execution_mode': 'continuous',
        'feedback_predecessor_id': 'verified-parent', 'feedback_session': seed,
        'plan': {'summary': 'Apply feedback', 'questions': [], 'tasks': [task]},
        'tasks': [{**task, 'status': 'pending'}],
        'runtime_configuration': svc.runtime_settings.get(),
    })
    svc.cancels[run['id']] = threading.Event()
    monkeypatch.setattr(svc, '_project_for_run', lambda _run: p)
    captured = []

    def execute(**kwargs):
        captured.append(kwargs)
        return {'execution_mode': 'continuous', 'worktree': p['workspace'],
            'branch': p['base_branch'], 'commit': 'a' * 40, 'tasks': [], 'checks': [],
            'known_cost_usd': 0.0, 'observed_cost_usd': 0.0,
            'session_id': 'prior-session'}

    svc.continuous_execute = execute
    monkeypatch.setattr(svc, '_independent_verify',
        lambda _rid, _run, _project, _configuration, artifacts:
            artifacts.update(verification={'verdict': 'pass', 'reason': 'verified'}))
    monkeypatch.setattr(svc, '_capture_capability', lambda _rid: None)

    svc._run(run['id'])

    assert len(captured) == 1
    forwarded = captured[0]['plan']['tasks'][0]
    assert forwarded['_feedback_session'] == seed
    assert forwarded['resume_feedback'] == 'Apply only the current feedback'
    assert 'resume_artifacts' not in captured[0]


def test_message_during_execution_is_automatically_planned_at_completion(app_env):
    import time
    client, store, svc, repo = app_env
    headers = login(client)
    project = create_project(client, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': 'Helper'}, headers=headers).json()['id']
    cid = client.post(f'/api/v4/agents/{aid}/conversations',
        json={'mode': 'do', 'project_id': project['id']}, headers=headers).json()['id']
    actor = svc.agents.conversation(cid)['actor_id']
    prior, _ = store.create_run(project['id'], 'Update greeting.txt', source={'type': 'agent', 'actor_id': actor})
    store.update(prior['id'], {'status': 'running', 'conversation_id': cid,
        'agent_id': aid, 'agent_version': 1, 'agent_snapshot': svc.agents.version(aid),
        'runtime_configuration': svc.runtime_settings.get()})
    svc.agents.attach_run(cid, prior['id'])
    response = client.post(f'/api/v4/conversations/{cid}/messages',
        json={'content': 'Also support Chinese greetings'}, headers=headers)
    assert response.status_code == 201
    assert response.json()['feedback_queued'] is True
    assert response.json()['conversation']['pending_feedback_count'] == 1
    assert len(store.all_runs()) == 1
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True,
                            capture_output=True, text=True).stdout.strip()
    store.update(prior['id'], {'status': 'ready_for_review', 'artifacts': {
        'worktree': str(repo), 'branch': 'main', 'commit': commit}})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        c = svc.agents.conversation(cid)
        successor = store.get(c['run_id'])
        if successor['id'] != prior['id'] and successor['status'] not in ('received', 'planning'):
            break
        time.sleep(.02)
    assert successor['id'] != prior['id']
    assert successor['status'] == 'awaiting_approval', successor
    assert successor['context']['commit_sha'] == commit
    assert c['pending_feedback_count'] == 0
    assert 'Chinese greetings' in successor['history'][-1]


def test_large_feedback_is_not_truncated_and_batches_remain_pending(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'ready_for_review')
    agents.settle_feedback(cid, rid)
    first = 'FIRST_REQUIREMENT' + 'x' * 40_000 + 'LAST_REQUIREMENT'
    agents.append_message(cid, 'user', first, feedback_status='pending')
    agents.append_message(cid, 'user', 'next' * 4_000, feedback_status='pending')
    successor = agents.adopt_feedback(cid)
    from factory.control.planning import build_prompt
    prompt = build_prompt(successor['request'], {'checks': {}, 'base_branch': 'main'}, successor['history'])
    assert first in prompt
    assert agents.conversation(cid)['pending_feedback_count'] == 1
    assert 'next' * 4_000 not in successor['request']


def test_clarification_crash_marker_prevents_replaying_applied_feedback(tmp_path):
    store, agents, cid, rid = setup(tmp_path, 'needs_clarification')
    ids = [m['id'] for m in agents.conversation(cid)['messages']]
    svc = Service(store, runner=object())
    svc.start_plan = lambda rid: None
    try:
        # Simulate API crash after clarify committed, before conversation settlement.
        svc.clarify(rid, 'Apply pending requirement', 'owner', feedback_message_ids=ids)
        agents.append_message(cid, 'user', 'Later feedback', feedback_status='pending')
        svc._drain_feedback()
        assert agents.conversation(cid)['pending_feedback_count'] == 1
        store.update(rid, {'status': 'ready_for_review'})
        successor = agents.adopt_feedback(cid)
        assert 'Later feedback' in successor['request']
        assert 'Keep the new output' not in successor['request']
    finally:
        svc.close()


def test_retry_attaches_conversation_and_queues_feedback_on_retry(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = create_project(client, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': 'Retry helper'}, headers=headers).json()['id']
    c = svc.agents.create_conversation(aid, 'do', p['id'], 1)
    prior, _ = store.create_run(p['id'], 'Maintain project', source={'type': 'agent', 'actor_id': 1})
    store.update(prior['id'], {'status': 'needs_human', 'conversation_id': c['id'],
        'agent_id': aid, 'agent_snapshot': svc.agents.version(aid)})
    svc.agents.attach_run(c['id'], prior['id'])
    monkeypatch.setattr(svc, 'start_plan', lambda rid: None)
    retried = svc.retry(prior['id'], 'owner', actor_id=1)
    assert svc.agents.conversation(c['id'])['run_id'] == retried['id']
    assert svc.retry(prior['id'], 'owner', actor_id=1)['id'] == retried['id']
    response = client.post(f"/api/v4/conversations/{c['id']}/messages",
        json={'content': 'Add CSV after repair'}, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()['feedback_queued']
    assert response.json()['run']['id'] == retried['id']
    assert len(store.all_runs()) == 2
    assert svc.agents.conversation(c['id'])['pending_feedback_count'] == 1


def test_execution_resume_keeps_new_scope_pending(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = create_project(client, repo, headers)
    aid = svc.agents.create({'name': 'Resume helper'}, 'owner')['id']
    c = svc.agents.create_conversation(aid, 'do', p['id'], 1)
    prior, _ = store.create_run(p['id'], 'Original scope', source={'type': 'agent', 'actor_id': 1})
    store.update(prior['id'], {'status': 'needs_human', 'conversation_id': c['id'],
        'plan': {'tasks': []}, 'artifacts': {'tasks': [{'id': 'finished', 'status': 'completed'}]}})
    svc.agents.attach_run(c['id'], prior['id'])
    svc.agents.append_message(c['id'], 'user', 'New feature on completed task', feedback_status='pending')
    def resume(rid, answer, *args):
        assert answer == '继续'
        return store.update(rid, {'status': 'queued'})
    monkeypatch.setattr(svc, 'continue_run', resume)
    response = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': '继续'}, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()['conversation']['pending_feedback_count'] == 1
    store.update(prior['id'], {'status': 'ready_for_review'})
    successor = svc.agents.adopt_feedback(c['id'])
    assert 'New feature on completed task' in successor['request']


def test_large_clarification_retains_latest_answer_and_unconsumed_feedback(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = create_project(client, repo, headers)
    aid = svc.agents.create({'name': 'Long helper'}, 'owner')['id']
    c = svc.agents.create_conversation(aid, 'do', p['id'], 1)
    original = 'original' + 'x' * 49_980
    pending = 'pending' + 'y' * 49_980
    answer = 'z' * 49_980 + 'FINAL_REQUIREMENT'
    prior, _ = store.create_run(p['id'], original, source={'type': 'agent', 'actor_id': 1})
    store.update(prior['id'], {'status': 'needs_clarification', 'conversation_id': c['id']})
    svc.agents.attach_run(c['id'], prior['id'])
    svc.agents.append_message(c['id'], 'user', pending, feedback_status='pending')
    monkeypatch.setattr(svc, 'start_plan', lambda rid: None)
    response = client.post(f"/api/v4/conversations/{c['id']}/messages", json={'content': answer}, headers=headers)
    assert response.status_code == 201, response.text
    updated = store.get(prior['id'])
    assert updated['request'] == answer
    assert original in updated['history']
    assert svc.agents.conversation(c['id'])['pending_feedback_count'] == 1


def test_retry_transaction_rolls_back_identity_and_attachment_together(tmp_path, monkeypatch):
    store, agents, cid, rid = setup(tmp_path, 'needs_human')
    store.update(rid, {'feedback_predecessor_id': 'verified-parent', 'feedback_applied_ids': ['receipt']})
    prior = store.get(rid)
    original_event = store._event
    def crash(*args, **kwargs):
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(store, '_event', crash)
    with pytest.raises(RuntimeError):
        agents.create_retry(prior, 'owner', 1, [])
    assert len(store.all_runs()) == 1
    assert agents.conversation(cid)['run_id'] == rid
    monkeypatch.setattr(store, '_event', original_event)
    retried, created = agents.create_retry(prior, 'owner', 1, [])
    assert created
    assert retried['feedback_predecessor_id'] == 'verified-parent'
    assert retried['feedback_applied_ids'] == ['receipt']
    assert agents.conversation(cid)['run_id'] == retried['id']
    assert agents.create_retry(prior, 'owner', 1, [])[0]['id'] == retried['id']


def test_clarification_and_feedback_preserve_original_authorization_risk(app_env, monkeypatch):
    client, store, svc, repo = app_env
    p = create_project(client, repo, login(client))
    aid = svc.agents.create({'name': 'Risk helper'}, 'owner')['id']
    c = svc.agents.create_conversation(aid, 'do', p['id'], 1)
    prior, _ = store.create_run(p['id'], 'Change authentication and deploy production', source={'type': 'agent', 'actor_id': 1})
    store.update(prior['id'], {'status': 'needs_clarification', 'conversation_id': c['id']})
    svc.agents.attach_run(c['id'], prior['id'])
    monkeypatch.setattr(svc, 'start_plan', lambda rid: None)
    run = svc.clarify(prior['id'], 'yes, keep the current appearance', 'owner')
    assert run['request'] == 'yes, keep the current appearance'
    plan = {'title': 'Small edit', 'summary': 'Update a file', 'questions': [], 'tasks': [{
        'id': 'edit', 'title': 'Edit file', 'prompt': 'Edit greeting.txt',
        'acceptance': ['file updated'], 'paths': ['greeting.txt'], 'checks': ['greeting'],
        'complexity': 'small', 'risk': 'low'}]}
    policy = {**svc.policies.get(p['id']), 'mode': 'autonomous', 'max_risk': 'medium'}
    reason, decision = svc._waiting_policy_check({**run, 'plan': plan}, p, policy)
    assert reason and decision['risk'] == 'high'
    assert decision['decision'] == 'human_approval'
    store.update(run['id'], {'status': 'ready_for_review'})
    svc.agents.append_message(c['id'], 'user', 'Adjust label', feedback_status='pending')
    successor = svc.agents.adopt_feedback(c['id'])
    assert 'authentication' in svc._authorization_request(successor)
    store.update(successor['id'], {'status': 'needs_human'})
    retried, _ = svc.agents.create_retry(store.get(successor['id']), 'owner', 1, [])
    assert 'authentication' in svc._authorization_request(retried)
