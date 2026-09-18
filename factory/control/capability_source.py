"""Capability source aggregation for a run.

Returns two distinct sections:
- loaded: Skills/modules actually loaded (from module_snapshot + session_skill_snapshot)
- invoked: Tools actually called (from real tool.call events in the events table)

These two categories are never merged. "Loaded" does not imply "invoked".
When no records exist (old runs), an explicit 'no_record' status is returned
instead of an empty list, to distinguish "nothing happened" from "we don't know".
"""
from __future__ import annotations

import json


def aggregate(store, run):
    """Return capability source summary for a given run.

    Returns:
        {
          "loaded": {
            "status": "available" | "no_record",
            "items": [{"name": str, "id": str, "origin": "project_module" | "session_skill"}]
          },
          "invoked": {
            "status": "available" | "no_record",
            "items": [{"name": str, "count": int, "first_at": str, "last_at": str}]
          }
        }
    """
    rid = run['id']

    # --- loaded ---
    module_snapshot = run.get('module_snapshot')
    session_skill_snapshot = run.get('session_skill_snapshot')

    has_loaded_record = module_snapshot is not None or session_skill_snapshot is not None

    loaded_items = []
    if module_snapshot is not None:
        for m in module_snapshot:
            loaded_items.append({
                'name': m.get('name', ''),
                'id': m.get('id', ''),
                'origin': 'project_module',
            })
    if session_skill_snapshot is not None:
        for s in session_skill_snapshot:
            loaded_items.append({
                'name': s.get('name', ''),
                'id': s.get('id', ''),
                'origin': 'session_skill',
            })

    loaded = {
        'status': 'available' if has_loaded_record else 'no_record',
        'items': loaded_items,
    }

    # --- invoked ---
    with store.connect() as db:
        rows = db.execute(
            "SELECT payload, at FROM events WHERE run_id=? AND type='tool.call' ORDER BY id",
            (rid,),
        ).fetchall()

    invoked_map: dict[str, dict] = {}
    for row in rows:
        payload = row['payload']
        if isinstance(payload, str):
            payload = json.loads(payload)
        name = payload.get('name', '未知工具')
        if name not in invoked_map:
            invoked_map[name] = {'name': name, 'count': 0, 'first_at': row['at'], 'last_at': row['at']}
        invoked_map[name]['count'] += 1
        invoked_map[name]['last_at'] = row['at']

    # Distinguish "modern run with no tool calls" from "old run with no records".
    # A modern run has at least one of the snapshot fields (even if empty list).
    # An old run has neither snapshot NOR any tool.call events.
    has_any_record = has_loaded_record or len(rows) > 0

    invoked = {
        'status': 'available' if has_any_record else 'no_record',
        'items': list(invoked_map.values()),
    }

    return {'loaded': loaded, 'invoked': invoked}
