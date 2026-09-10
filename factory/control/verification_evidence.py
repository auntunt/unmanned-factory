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
