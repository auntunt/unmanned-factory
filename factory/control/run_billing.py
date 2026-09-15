"""Run usage reconciliation, budget ceilings and accounting for interrupted provider calls."""
from __future__ import annotations

import re
import subprocess

from factory.control.autonomy import all_events, valid_cost
from factory.control.budget import BudgetConfigurationError, dollar_budget
from factory.control.recovery import _failed_platform_checks, _has_successful_command_evidence
from factory.control.store import Conflict, scrub


_LEGACY_CLAUDE_BUDGET_ERROR = re.compile(
    r'provider SDK failure: Claude Code returned an error result: '
    r'Reached maximum budget \(\$(\d+(?:\.\d+)?)\) \(exit code: 1\)')


def _verification_reserve_usd(remaining_usd):
    """Keep a bounded part of a finite run budget for independent review.

    The reserve is deliberately meaningful for the default $10 run while still
    leaving at least half of very small budgets to the coding turn. It is a
    provider-side ceiling allocation, not a second charge or a ledger entry.
    """
    if remaining_usd is None:
        return 0.0
    remaining = float(remaining_usd)
    return min(2.0, remaining / 2.0, max(0.25, remaining * 0.20))


def _usage(self, rid, *, profile=None):
    calls = self._reconciled_usage_calls(rid, profile=profile)
    costs = [call['cost_usd'] for call in calls]
    total = sum(cost for cost in costs if cost is not None)
    if not __import__('math').isfinite(total):
        raise Conflict('累计费用超出可表示范围，停止派发', error_type='budget')
    return {'known_cost_usd': total, 'unknown_cost_calls': sum(cost is None for cost in costs), 'calls': len(costs)}


def _reconciled_usage_calls(self, rid, *, profile=None):
    """Collapse durable accounting rows into logical provider calls."""
    grouped = {}
    order = []
    for event in all_events(self.store, rid):
        if event['type'] != 'usage.recorded':
            continue
        payload = event['payload'] if isinstance(event.get('payload'), dict) else {}
        if profile is not None and payload.get('profile') != profile:
            continue
        call_id = payload.get('call_id')
        key = ('call', call_id) if isinstance(call_id, str) and call_id else ('event', event['id'])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(payload)
    calls = []
    for key in order:
        payloads = grouped[key]
        known = [valid_cost(payload.get('cost_usd')) for payload in payloads]
        known = [cost for cost in known if cost is not None]
        ceilings = [valid_cost(payload.get('max_budget_usd')) for payload in payloads]
        ceilings = [ceiling for ceiling in ceilings if ceiling is not None]
        calls.append({'profile': payloads[-1].get('profile'), 'call_id': key[1] if key[0] == 'call' else None,
                      'cost_usd': known[-1] if known else None,
                      'max_budget_usd': max(ceilings) if ceilings else None})
    return calls


def _budget_usage(self, rid, project):
    """Build a pessimistic ledger for calls whose invoice is unresolved.

        A later row with the same call id and a known cost reconciles the hold.
        Historical rows without a recorded ceiling reserve the whole finite run
        budget, because allowing another paid call would make the hard cap
        unenforceable after a restart.
        """
    calls = self._reconciled_usage_calls(rid)
    calls = [call for call in calls if call.get('profile') != 'requirement_analysis']
    known = sum(call['cost_usd'] for call in calls
                if call['cost_usd'] is not None)
    unresolved = [call['max_budget_usd'] for call in calls
                  if call['cost_usd'] is None]
    if not __import__('math').isfinite(known):
        raise Conflict('累计费用超出可表示范围，停止派发', error_type='budget')
    limit = valid_cost(project.get('budget_usd'))
    reserved = sum(ceiling if ceiling is not None else (limit or 0.0)
                   for ceiling in unresolved)
    if not __import__('math').isfinite(reserved):
        raise Conflict('未对账调用的预算占用超出可表示范围，停止派发', error_type='budget')
    return {'known_cost_usd': known,
            'unknown_cost_calls': len(unresolved),
            'unknown_cost_reserved_usd': reserved,
            'effective_cost_usd': known + reserved}


def _dollar_budget(self, rid, project):
    """Read durable usage immediately before a paid project call."""
    try:
        usage = self._budget_usage(rid, project)
        limit = project.get('budget_usd')
        if limit is not None:
            limit += self.store.get(rid).get('budget_credit_usd', 0)
        return dollar_budget(limit, {
            'known_cost_usd': usage['effective_cost_usd'],
            'unknown_cost_calls': usage['unknown_cost_calls'],
        })
    except BudgetConfigurationError as exc:
        raise Conflict(f'项目预算配置无效：{exc}', error_type='budget') from None


def _remaining_dollar_budget(self, rid, project):
    budget = self._dollar_budget(rid, project)
    if budget.exhausted:
        usage = self._budget_usage(rid, project)
        hold = usage['unknown_cost_reserved_usd']
        detail = (f'，未对账调用按上限暂占 ${hold:.4f}' if hold else '')
        raise Conflict(f'本次运行预算已用尽：已记录 ${usage["known_cost_usd"]:.4f}{detail}，上限 ${budget.limit_usd:.4f}；停止新的模型调用', error_type='budget')
    return budget


def _budget_stop_artifacts(self, rid, project, reason, artifacts=None):
    """Persist a structured, user-visible reason for every service budget stop."""
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    message = scrub(str(reason))[:2000]
    usage = self._usage(rid)
    budget_usage = self._budget_usage(rid, project)
    artifacts.update(total_known_cost_usd=usage['known_cost_usd'],
                     total_unknown_cost_reserved_usd=budget_usage['unknown_cost_reserved_usd'],
                     budget_usd=project.get('budget_usd'), budget_exhausted=True,
                     autopublish_blocked=True, needs_human=message)
    return artifacts


def _legacy_claude_budget_stop(self, rid, artifacts, project):
    """Recognize the one pre-classification Claude ceiling failure safely."""
    if (not isinstance(artifacts, dict) or artifacts.get('commit')
            or _failed_platform_checks(artifacts)
            or not _has_successful_command_evidence(artifacts)):
        return False
    limit = valid_cost(project.get('budget_usd'))
    if limit is None or self._usage(rid)['known_cost_usd'] < limit:
        return False
    tasks = artifacts.get('tasks') or []
    attempts = [attempt for task in tasks for attempt in task.get('attempts') or []
                if isinstance(attempt, dict)]
    errors = [artifacts.get('error'),
              *(task.get('error') for task in tasks if isinstance(task, dict)),
              *(attempt.get('error') for attempt in attempts)]
    matches = (_LEGACY_CLAUDE_BUDGET_ERROR.fullmatch(error)
               for error in errors if isinstance(error, str))
    match = next((candidate for candidate in matches if candidate is not None), None)
    if match is None:
        return False
    reported_limit = valid_cost(match.group(1))
    if reported_limit is None or not __import__('math').isclose(
            reported_limit, limit, rel_tol=0.0, abs_tol=1e-9):
        return False
    session_ids = [artifacts.get('session_id'),
                   *(task.get('session_id') for task in tasks if isinstance(task, dict)),
                   *(attempt.get('session_id') for attempt in attempts)]
    if not any(isinstance(value, str) and value.strip() for value in session_ids):
        return False
    worktree = artifacts.get('worktree')
    if not isinstance(worktree, str) or not worktree:
        return False
    try:
        status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=all'],
            cwd=worktree, check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(status.stdout.strip())


def _record_interrupted_provider_usage(self, run):
    """Close each in-flight provider lane with a durable unknown-cost hold."""
    rid = run['id']
    events = list(all_events(self.store, rid))
    latest_by_lane = {}
    for event in events:
        if event['type'] != 'provider.started':
            continue
        payload = event['payload'] if isinstance(event.get('payload'), dict) else {}
        lane = (event.get('task_id'), payload.get('profile'))
        latest_by_lane[lane] = event
    if not latest_by_lane:
        return
    project = self.store.project(run['project_id'])
    fallback_ceiling = valid_cost(project.get('budget_usd'))
    for lane, started in latest_by_lane.items():
        payload = started['payload']
        call_id = payload.get('call_id')
        reconciled = any(event['type'] == 'usage.recorded'
            and event['id'] > started['id']
            and ((isinstance(call_id, str) and call_id
                  and event['payload'].get('call_id') == call_id)
                 or ((event.get('task_id'), event['payload'].get('profile')) == lane))
            for event in events)
        if reconciled:
            continue
        ceiling = valid_cost(payload.get('max_budget_usd'))
        self.store.append(rid, 'usage.recorded', {
            **payload, 'cost_usd': None,
            'max_budget_usd': ceiling if ceiling is not None else fallback_ceiling,
            'interrupted': True,
            'message': '模型调用在服务中断时未完成费用对账，暂按调用上限占用预算',
        }, started.get('task_id'))
