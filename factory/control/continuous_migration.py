"""Offline, administrator-invoked migration of paused DAG evidence.

No database writes, provider calls, or mutation of existing worktrees/branches.
Failures preserve the new worktree and original sources for manual reconciliation.
"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path

from factory.control.continuous import _guard_snapshot
from factory.control.execution import (
    ExecutionError, _baseline, _check_argv, _git_ok, _restore_draft, _safe_ref_part, _status_paths,
)
from factory.control.model_routing import select_profile
from factory.control.planning import continuous_plan as make_plan


def _ordered_tasks(plan):
    raw = plan.get('tasks')
    if not isinstance(raw, list) or not raw:
        raise ExecutionError('legacy plan has no tasks')
    tasks = {}
    for task in raw:
        ident = _safe_ref_part(task.get('id', ''), 'legacy task id')
        if ident in tasks:
            raise ExecutionError('duplicate legacy task id')
        tasks[ident] = task
    ordered, visiting, seen = [], set(), set()
    def visit(ident):
        if ident in visiting or ident not in tasks:
            raise ExecutionError('legacy task dependencies are invalid or cyclic')
        if ident in seen:
            return
        visiting.add(ident)
        for dependency in tasks[ident].get('depends_on', []):
            visit(dependency)
        visiting.remove(ident)
        seen.add(ident)
        ordered.append(tasks[ident])
    for ident in tasks:
        visit(ident)
    return ordered


def migrate_to_continuous(*, run_id, plan, project, profiles, artifacts, emit=None, timeout_s=300):
    """Return ``(single_task_plan, continuous_artifacts)`` for explicit adoption.

    The caller must stop the old worker before calling and persist the returned
    state only on success. A conflict exception carries the preserved migration
    worktree in ``exc.artifacts``; it must not be used as a successful migration.
    """
    run_part = _safe_ref_part(run_id, 'run id')
    ordered = _ordered_tasks(plan)
    workspace = Path(project['workspace']).expanduser().resolve()
    base = artifacts.get('base_sha')
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40,64}', base):
        raise ExecutionError('legacy artifacts lack a valid original baseline')
    current = _git_ok(workspace, 'rev-parse', '--verify', f"refs/heads/{project['base_branch']}^{{commit}}", timeout_s=timeout_s)
    if current != base:
        raise ExecutionError('original project baseline changed; migration requires review')
    _check_argv(project, project.get('checks', {}).keys())
    if not project.get('checks'):
        raise ExecutionError('migration requires configured final checks')
    integration = artifacts.get('integration_worktree') or artifacts.get('worktree')
    if integration and Path(integration).is_dir() and _status_paths(Path(integration), timeout_s=timeout_s):
        raise ExecutionError('legacy integration workspace contains a draft; reconcile it explicitly before migration')
    states = {}
    task_ids = {task['id'] for task in ordered}
    for state in artifacts.get('tasks') or []:
        ident = state.get('id')
        if ident in states:
            raise ExecutionError('duplicate legacy task evidence')
        if ident not in task_ids and (state.get('commit') or state.get('worktree')):
            raise ExecutionError('legacy artifacts contain work outside the saved plan; reconcile it before migration')
        states[ident] = state
    completed = [task['id'] for task in ordered if states.get(task['id'], {}).get('status') in ('verified', 'completed')]
    for task in ordered:
        if task['id'] in completed and any(dep not in completed for dep in task.get('depends_on', [])):
            raise ExecutionError('verified task has an unfinished dependency')
    unfinished = [task['id'] for task in ordered if task['id'] not in completed]
    intent = (
        'Continue this existing project from the preserved implementation below. '
        'The old DAG is background, not separate execution sessions. Keep completed work, '
        'inspect restored drafts, finish every remaining requirement, and run final regression checks. '
        'Completed task IDs: ' + json.dumps(completed) + '. Unfinished task IDs: ' + json.dumps(unfinished) + '.\n'
        'COMPLETE ORIGINAL PLAN (all task prompts and acceptance criteria, do not omit requirements):\n' +
        json.dumps(plan, ensure_ascii=False, indent=2))
    new_plan = make_plan(intent, project)
    task = new_plan['tasks'][0]
    task['title'] = '接续已有成果并完成全部需求'
    task['risk'] = 'high' if any(t.get('risk') == 'high' for t in ordered) else 'medium' if any(t.get('risk') == 'medium' for t in ordered) else 'low'
    task['acceptance'] = [criterion for old in ordered for criterion in old.get('acceptance', [])] or task['acceptance']
    # Preserve every requirement or fail explicitly; never silently truncate an import.
    if len(json.dumps(new_plan, ensure_ascii=False).encode()) > 900_000:
        raise ExecutionError('legacy plan is too large for one provider request; reconcile explicitly')
    route = select_profile(task, profiles, attempt=1, auto_escalate=False)
    nonce = uuid.uuid4().hex[:10]
    branch = f'factory/{run_part}-continuous-{nonce}'
    parent = Path(tempfile.mkdtemp(prefix=f'.factory-{run_part}-migration-', dir=workspace.parent))
    destination = parent / 'coding'
    report = {'source_run_id': run_id, 'completed_task_ids': completed, 'unfinished_task_ids': unfinished,
              'source_worktrees': {key: value.get('worktree') for key, value in states.items() if value.get('worktree')},
              'source_commits': {key: value.get('commit') for key, value in states.items() if value.get('commit')},
              'restored_commits': [], 'restored_drafts': [], 'status': 'migrating'}
    output = {'execution_mode': 'continuous', 'base_sha': base, 'branch': branch,
              'worktree': str(destination), 'commit': None, 'checks': [], 'tasks': [],
              'session_id': None, 'session_profile': dict(route), 'execution_checks': project['checks'],
              'known_cost_usd': 0.0, 'observed_cost_usd': 0.0, 'migration': report}
    def notify(kind):
        if emit:
            emit(kind, json.loads(json.dumps(output)), None)
    try:
        _git_ok(workspace, 'worktree', 'add', '-q', '-b', branch, str(destination), base, timeout_s=timeout_s)
        for old in ordered:
            ident = old['id']
            if ident not in completed:
                continue
            commit = states[ident].get('commit')
            if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40,64}', commit):
                raise ExecutionError(f'verified task {ident} lacks a valid commit')
            source = states[ident].get('worktree')
            if source and Path(source).is_dir() and _status_paths(Path(source), timeout_s=timeout_s):
                raise ExecutionError(f'verified task {ident} also has a dirty draft; reconcile it explicitly')
            _git_ok(workspace, 'merge-base', '--is-ancestor', base, commit, timeout_s=timeout_s)

            _git_ok(destination, '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                    '-c', 'user.name=Factory', '-c', 'user.email=factory@localhost',
                    'cherry-pick', commit, timeout_s=timeout_s)
            report['restored_commits'].append({'task_id': ident, 'source_commit': commit,
                'commit': _git_ok(destination, 'rev-parse', 'HEAD', timeout_s=timeout_s)})
        integrated_commit = artifacts.get('commit') or artifacts.get('current_commit')
        if integrated_commit:
            if not isinstance(integrated_commit, str) or not re.fullmatch(r'[0-9a-f]{40,64}', integrated_commit):
                raise ExecutionError('legacy integration commit is invalid')
            unmatched = _git_ok(workspace, 'cherry', branch, integrated_commit, base, timeout_s=timeout_s)
            if any(line.startswith('+') for line in unmatched.splitlines()):
                raise ExecutionError('legacy integration contains additional commits outside verified task evidence')
        for old in ordered:
            ident = old['id']
            prior = states.get(ident, {})
            if ident in completed or not prior.get('worktree'):
                continue
            # _restore_draft validates source repository/ref, rejects unsafe paths,
            # and refuses to overwrite conflicting untracked files in the destination.
            source_head = _git_ok(Path(prior['worktree']), 'rev-parse', 'HEAD', timeout_s=timeout_s)
            unaccounted = _git_ok(workspace, 'cherry', branch, source_head, base, timeout_s=timeout_s)
            if any(line.startswith('+') for line in unaccounted.splitlines()):
                raise ExecutionError(f'unfinished task {ident} contains unverified commits; preserve and reconcile explicitly')
            _restore_draft(workspace, prior, destination, timeout_s)
            report['restored_drafts'].append(ident)
        output['workspace_guard'] = _guard_snapshot(_baseline(destination))
        output['tasks'] = [{'id': task['id'], 'title': task['title'], 'status': 'queued',
            'profile': route['profile'], 'branch': branch, 'worktree': str(destination),
            'commit': None, 'checks': [], 'attempts': [], 'session_id': None}]
        # This is an unverified draft, even though individual source commits passed checks.
        report['status'] = 'prepared'
        (parent / 'migration-report.json').write_text(json.dumps({'plan': new_plan, 'artifacts': output}, ensure_ascii=False, indent=2), encoding='utf-8')
        notify('execution.migration_prepared')
        return new_plan, output
    except Exception as exc:
        report.update(status='conflict_or_failed', error=str(exc))
        (parent / 'migration-report.json').write_text(json.dumps({'plan': new_plan, 'artifacts': output}, ensure_ascii=False, indent=2), encoding='utf-8')
        # Keep both sides and any conflict markers; never abort/reset the source work.
        raise ExecutionError('continuous migration stopped; original work and new reconciliation workspace preserved: ' + str(exc), artifacts=output) from exc
