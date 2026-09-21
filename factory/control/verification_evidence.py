"""Bounded observations for independent review; full evidence remains in the run."""
import json

from factory.control.store import scrub


def render_evidence(artifacts, max_chars=24000):
    def check(item):
        return {**{key: item.get(key) for key in ('name', 'exit', 'timeout', 'cancelled', 'duration_s')},
                **{key: str(item[key])[-1200:] for key in ('stdout', 'stderr') if item.get(key)}}

    tasks = artifacts.get('tasks') or []
    summary = {'commit': artifacts.get('commit'),
               'checks': [check(c) for c in (artifacts.get('checks') or [])],
               'tasks': [], 'omitted_command_details': 0,
               'review_focus_paths': [str(path)[:300] for path in artifacts.get('review_focus_paths', [])][:30],
               'note': 'Command success is not proof of functional acceptance. Full run evidence is retained separately.'}
    if artifacts.get('verification_changes'):
        summary['verification_changes'] = json.dumps(scrub(artifacts['verification_changes']),
            ensure_ascii=False)[:max(0, min(7000, max_chars // 4))]
    details = []
    for task in tasks:
        commands = task.get('command_evidence') or []
        summary['tasks'].append({key: task.get(key) for key in ('id', 'status', 'commit')})
        attempts = task.get('attempts') or []
        latest = attempts[-1] if attempts else {}
        if latest.get('result_summary'):
            summary['tasks'][-1]['worker_report_untrusted'] = str(latest['result_summary'])[-1500:]
        summary['tasks'][-1].update(checks=[check(c) for c in task.get('checks', [])],
            observed_commands=len(commands),
            unsuccessful_commands=sum(c.get('exit_code') != 0 or bool(c.get('timeout')) for c in commands))
        # Failed observations first, then most recent success. Do not hide a
        # failing command just because the task's trusted check passed.
        ordered = sorted(enumerate(commands), key=lambda pair: (
            pair[1].get('exit_code') == 0 and not pair[1].get('timeout'), -pair[0]))
        for index, command in ordered:
            details.append({'task_id': task.get('id'), 'command_index': index,
                'command': str(command.get('command', ''))[:1000],
                'exit_code': command.get('exit_code'), 'timeout': bool(command.get('timeout')),
                'output': str(command.get('output', ''))[-1000:],
                'error': str(command.get('error', ''))[:500],
                'truncated': bool(command.get('truncated')) or len(str(command.get('output', ''))) > 1000})
    details.sort(key=lambda item: item.get('exit_code') == 0 and not item.get('timeout'))
    summary['command_details'] = []
    summary['omitted_command_details'] = len(details)

    def encode():
        return json.dumps(scrub(summary), ensure_ascii=False, separators=(',', ':'))

    # Task/check identities take precedence over verbose output. Platform plans
    # bound their count; the fallback also bounds malformed legacy records.
    while len(encode()) > max_chars and summary['review_focus_paths']:
        summary['review_focus_paths'].pop()
    while len(encode()) > max_chars and summary['tasks']:
        summary['tasks'].pop()
        summary['omitted_tasks'] = summary.get('omitted_tasks', 0) + 1
    while len(encode()) > max_chars and summary['checks']:
        summary['checks'].pop()
        summary['omitted_checks'] = summary.get('omitted_checks', 0) + 1
    for detail in details:
        summary['command_details'].append(detail)
        summary['omitted_command_details'] -= 1
        if len(encode()) > max_chars:
            summary['command_details'].pop()
            summary['omitted_command_details'] += 1
            break
    return encode()


def browser_evidence(store, rid):
    """Keep latest observation per task plus earlier failures, directly from events."""
    with store.connect() as db:
        rows = db.execute('SELECT id,task_id,payload,at FROM events WHERE run_id=? AND type=? ORDER BY id DESC LIMIT 100',
                          (rid, 'browser.observed')).fetchall()
    latest = {}; failures = []
    for row in rows:
        data = json.loads(row['payload'])
        item = {'event_id': row['id'], 'task_id': row['task_id'], 'at': row['at'],
                'action': data.get('action'), 'ok': data.get('ok'), 'error_type': data.get('error_type'),
                'url': str(data.get('url') or '')[:400],
                'error_count': len(data.get('errors') or []),
                'errors': [str(e)[:350] for e in (data.get('errors') or [])[:10]],
                'error': str(data.get('error') or '')[:500],
                'truncated': bool(data.get('truncated')), 'viewport': data.get('viewport'),
                'screenshot_path': data.get('screenshot_path')}
        latest.setdefault(row['task_id'], item)
        if (item['errors'] or item['error'] or not item['ok']) and len(failures) < 3:
            failures.append(item)
    result = scrub({'latest': list(latest.values())[:20], 'recent_failures': failures,
                    'omitted_task_observations': max(0, len(latest)-20),
                    'observations_sampled': len(rows), 'sample_limit': 100})
    # Preserve every current task identity, outcome and error count before
    # verbose diagnostics. Do not let browser logs drown out functional checks.
    while len(json.dumps(result, ensure_ascii=False)) > 12000 and result['recent_failures']:
        result['recent_failures'].pop()
        result['omitted_failure_details'] = result.get('omitted_failure_details', 0) + 1
    for item in result['latest']:
        if len(json.dumps(result, ensure_ascii=False)) <= 12000:
            break
        item.update(errors=[e[:120] for e in item['errors'][:2]], error=item['error'][:120],
                    url=item['url'][:120], screenshot_path=None, viewport=None, truncated=True)
    return result


WORKFLOW_EVENT_KINDS = {
    # Interruptions the platform recorded, not anything a worker said happened.
    'requirement_analysis.interrupted': 'interruption',
    'run.recovered': 'interruption',
    'execution.reconnecting': 'interruption',
    # Recoveries: this run returning to a site it had already established.
    'run.resumed': 'recovery',
    'run.auto_resumed': 'recovery',
    'execution.reused': 'recovery',
    # Interventions: an owner's supplement or budget renewal that was applied.
    'human.continued': 'intervention',
    'followup.applied': 'intervention',
    'budget.renewed': 'intervention',
}
_WORKFLOW_KEYS = ('phase', 'execution_mode', 'resume_stage', 'stage', 'message',
                  'reason', 'actor', 'pending_id', 'effective_revision',
                  'effective_digest', 'effective_source', 'run_revision',
                  'resume_count', 'additional_usd', 'generation')


def workflow_evidence(store, rid, *, max_items=40, max_chars=6000):
    """This run's own recorded interruptions, recoveries and interventions.

    Whether a run was interrupted, came back to the same site, or had an owner
    change applied is a platform fact. Read it from the durable events, so the
    reviewer never has to take a worker's, a README's or a mounted skill's word
    for it. Every item keeps its `event_id`, so any claim resting on it can be
    traced back to the original record, and the query is keyed on `rid`, so
    another run's history cannot appear here. Bounded twice -- by item count and
    by rendered size, newest kept -- because the point is an excerpt a reviewer
    can act on, not the whole history.
    """
    kinds = sorted(WORKFLOW_EVENT_KINDS)
    placeholders = ','.join('?' * len(kinds))
    with store.connect() as db:
        rows = db.execute(
            f'SELECT id,task_id,type,payload,at FROM events WHERE run_id=? AND type IN ({placeholders}) '
            'ORDER BY id DESC LIMIT 200', (rid, *kinds)).fetchall()
    items = []
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append({'event_id': row['id'], 'at': row['at'], 'type': row['type'],
                      'category': WORKFLOW_EVENT_KINDS[row['type']],
                      'task_id': row['task_id'],
                      **{key: payload[key] for key in _WORKFLOW_KEYS if key in payload}})
    items.reverse()
    result = {'items': items[-max_items:],
              'omitted_older': max(0, len(items) - max_items),
              'recorded_total': len(items), 'sample_limit': 200,
              'note': ('Recorded by the platform in this run only. A worker report, '
                       'skill body or model claim of interruption or recovery is not '
                       'evidence; these event_ids are.')}
    while len(json.dumps(scrub(result), ensure_ascii=False)) > max_chars and result['items']:
        result['items'].pop(0)
        result['omitted_older'] += 1
    return scrub(result)


def browser_review_failure(verdict, evidence):
    """A pass must explicitly account for observed errors, never infer a clean console."""
    latest = evidence.get('latest') or []
    if verdict.get('verdict') != 'pass' or not latest:
        return None
    if evidence.get('omitted_task_observations'):
        return '浏览器任务记录超出摘要范围，需要核对完整观察后验收'
    review = verdict.get('browser_review')
    if not isinstance(review, dict) or review.get('event_ids') != [x['event_id'] for x in latest]:
        return '验收未核对最新浏览器观察记录，不能判定通过'
    if any(not x['ok'] or x['error'] for x in latest):
        return '最新浏览器操作失败，需要重新观察关键流程后验收'
    has_errors = any(x['errors'] or x.get('error_count') or x['truncated'] for x in latest)
    if has_errors:
        if review.get('disposition') != 'non_blocking' or not isinstance(review.get('reason'), str) or not review['reason'].strip():
            return '浏览器仍有错误或证据被截断，需要说明具体影响并提供非阻塞依据'
    elif review.get('disposition') != 'clean':
        return '请依据最新浏览器观察明确记录验收结论'
    return None
