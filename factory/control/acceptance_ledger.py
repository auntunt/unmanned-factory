"""Acceptance coverage belongs to the controller, not a model's total count."""
from factory.control import effective_contract
from factory.control.fidelity import criteria as fidelity_criteria
import hashlib
import json


def criteria_for(run):
    contract = effective_contract.current(run)
    # The plan was written under the agreement in force when it was planned, so a
    # requirement the owner has since replaced can still be contradicted by the
    # plan's own acceptance line. Only the lines a change analysis explicitly
    # overturned -- each quoted byte for byte -- are dropped; every other plan
    # constraint still stands.
    overturned = effective_contract.superseded_plan_ids(contract)
    result = [row for row in effective_contract.plan_criteria(run)
              if row['id'] not in overturned]
    if not result:
        # Every stated row was overturned; the agreement's own rows carry acceptance
        # from here, and an empty ledger must never read as "nothing to verify".
        result = [{'id': 'request:1', 'task_id': None,
                   'text': run.get('root_request') or run.get('request', '')}]
    # The requirement rows come from the run's effective agreement, so a forbidden
    # zone the owner has since lifted is no longer a criterion to fail against and
    # an authorized addition is one to verify. A run that was never revised gets
    # exactly the confirmed specification, unchanged.
    if contract:
        result.extend({'id': f'requirement:{i}', 'task_id': None, 'class': 'requirement', 'text': text}
                      for i, text in enumerate(effective_contract.criteria_texts(contract))
                      if text.strip())
    result.extend(fidelity_criteria(run))
    return result


def coverage(criteria, verdict, commit, *, skills=()):
    # Some providers use the descriptive alias despite the requested `criteria`
    # key. Accept the same row schema, never choose silently between conflicts.
    conflicting = ('criteria' in verdict and 'acceptance_coverage' in verdict
                   and verdict['criteria'] != verdict['acceptance_coverage'])
    rows = verdict.get('criteria', verdict.get('acceptance_coverage', []))
    invalid_rows = conflicting or not isinstance(rows, list) or len(rows) > len(criteria)
    if invalid_rows:
        rows = []
    valid_ids = {c['id'] for c in criteria}
    allowed_skills = {(s['id'],s['version']) for s in skills}
    by_id, invalid = {}, invalid_rows
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
