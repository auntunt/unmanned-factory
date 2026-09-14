"""Acceptance coverage belongs to the controller, not a model's total count."""
import hashlib
import json


def criteria_for(run):
    result = []
    for task_index, task in enumerate((run.get('plan') or {}).get('tasks', [])):
        task_id = task.get('id') or f'legacy-{task_index + 1}'
        for index, text in enumerate(task.get('acceptance') or []):
            result.append({'id': f'task:{task_id}:{index + 1}', 'task_id': task_id, 'text': text})
    for index, text in enumerate((run.get('agent_snapshot') or {}).get('acceptance') or []):
        result.append({'id': f'agent:{index + 1}', 'task_id': None, 'text': text})
    if not result:
        result = [{'id': 'request:1', 'task_id': None, 'text': run.get('root_request') or run.get('request', '')}]
    return result


def coverage(criteria, verdict, commit, *, skills=()):
    rows = verdict.get('criteria', [])
    if not isinstance(rows, list) or len(rows) > len(criteria):
        rows = []
    valid_ids = {c['id'] for c in criteria}
    allowed_skills = {(s['id'],s['version']) for s in skills}
    by_id, invalid = {}, False
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or row.get('id') not in valid_ids
                or row['id'] in by_id or row.get('status') not in ('pass', 'fail', 'unverified')
                or not isinstance(row.get('evidence'), str) or len(row['evidence']) > 3000):
            invalid = True
            continue
        refs = row.get('skill_refs', [])
        if (not isinstance(refs,list) or len(refs)>24 or any(not isinstance(s,dict) or set(s)!={'id','version'} or not isinstance(s.get('id'),str) or type(s.get('version')) is not int or (s['id'],s['version']) not in allowed_skills for s in refs)):
            invalid = True
            continue
        by_id[row['id']] = row
    items = []
    for criterion in criteria:
        row = by_id.get(criterion['id'], {})
        evidence = row.get('evidence', '').strip()
        status = row.get('status', 'unverified') if evidence else 'unverified'
        items.append({**criterion, 'status': status, 'evidence': evidence, **({'skill_refs':row['skill_refs']} if 'skill_refs' in row else {})})
    counts = {status: sum(i['status'] == status for i in items) for status in ('pass', 'fail', 'unverified')}
    digest = hashlib.sha256(json.dumps(criteria, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'schema_version': 1, 'commit': commit, 'criteria_digest': digest, 'items': items,
            'counts': counts, 'total': len(items), 'accounted': not invalid and len(by_id) == len(criteria) and all(i['evidence'] for i in items), 'complete': not invalid and counts['pass'] == len(items)}


def repair_guidance(reason):
    return ('Reproduce the specific failure before changing code. Identify the shared rule or component and inspect '
            'its callers and sibling cases for the same defect. Repair confirmed affected cases, preserve unrelated '
            'behavior, and add a regression that fails for the original bug. If the check itself is wrong, prove that '
            'against the user contract rather than weakening acceptance. Reuse still-valid evidence; do not repeat '
            'a whole-project audit without a changed shared dependency or new failure. Finding:\n' + reason)
