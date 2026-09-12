"""Explicit model fixture for the per-criterion verification response contract."""
import json


def passing_review(request, reason):
    criteria = json.loads(request.prompt.split('CRITERIA JSON:\n', 1)[1].split('\nEND CRITERIA', 1)[0])
    return json.dumps({'verdict': 'pass', 'reason': reason,
        'criteria': [{'id': item['id'], 'status': 'pass', 'evidence': reason} for item in criteria]})
