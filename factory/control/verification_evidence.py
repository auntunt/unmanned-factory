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
                'action': data.get('action'), 'ok': data.get('ok'),
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
