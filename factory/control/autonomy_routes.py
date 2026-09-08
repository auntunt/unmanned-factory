"""Authenticated policy, operations overview and portable evidence exports."""
from __future__ import annotations

import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from factory.control.autonomy import PolicyStore, valid_cost
from factory.control.engineering_overview import engineering_overview
from factory.control.store import now


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    mode: Literal['supervised', 'autonomous']
    max_risk: Literal['low', 'medium', 'high']
    max_attempts: int = Field(ge=1, le=3, strict=True)
    auto_escalate: bool = Field(strict=True)
    resume_on_restart: bool = Field(strict=True)


def overview(store):
    from factory.control.capabilities import CapabilityStore
    projects = store.projects()
    names = {p['id']: p['name'] for p in projects}
    runs = store.all_runs()
    capabilities = CapabilityStore(store).list()
    by_id = {r['id']: r for r in runs}
    usage = {}
    known = 0.0
    unknown_runs = set()
    accounted = set()
    with store.connect() as db:
        rows = db.execute("SELECT run_id,payload FROM events WHERE type='usage.recorded' ORDER BY id")
        for row in rows:
            payload = json.loads(row['payload'])
            key = (str(payload.get('profile') or 'unknown'), str(payload.get('model') or '未记录型号'),
                   str(payload.get('provider') or '未记录供应商'))
            item = usage.setdefault(key, {'profile': key[0], 'model': key[1], 'provider': key[2], 'calls': 0,
                                         'known_cost_usd': 0.0, 'unknown_cost_calls': 0})
            cost = valid_cost(payload.get('cost_usd'))
            item['calls'] += 1
            accounted.add(row['run_id'])
            if cost is None:
                item['unknown_cost_calls'] += 1
                unknown_runs.add(row['run_id'])
            elif math.isfinite(known + cost) and math.isfinite(item['known_cost_usd'] + cost):
                item['known_cost_usd'] += cost
                known += cost
            else:
                item['unknown_cost_calls'] += 1
                unknown_runs.add(row['run_id'])
        latest = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 30')]
    # Historical v2 totals remain visible without inventing missing call counts.
    legacy_known = 0.0
    for run in runs:
        if run['id'] not in accounted:
            artifacts = run.get('artifacts') or {}
            cost = valid_cost(artifacts.get('total_known_cost_usd', artifacts.get('known_cost_usd', artifacts.get('observed_cost_usd'))))
            if cost is not None and math.isfinite(known + cost):
                known += cost
                legacy_known += cost
            if artifacts.get('billing_incomplete'):
                unknown_runs.add(run['id'])
    recent = []
    for event in latest:
        run = by_id.get(event['run_id'], {})
        event['payload'] = json.loads(event['payload'])
        recent.append({**event, 'version': 1,
                       'project_name': names.get(run.get('project_id'), '项目'),
                       'run_title': (run.get('plan') or {}).get('title') or run.get('request', '')[:100]})
    attention_states = {'needs_clarification', 'awaiting_approval', 'needs_human', 'failed'}
    attention = []
    for run in runs:
        if run['status'] not in attention_states or len(attention) >= 10:
            continue
        triage = run.get('triage') or {}
        reason = '；'.join(triage.get('questions') or triage.get('reasons') or [])
        if run['status'] in ('needs_human', 'failed'):
            with store.connect() as db:
                row = db.execute("SELECT payload FROM events WHERE run_id=? AND type IN ('run.failed','run.recovered') ORDER BY id DESC LIMIT 1", (run['id'],)).fetchone()
            if row:
                reason = json.loads(row['payload']).get('message', reason)
        attention.append({'id': run['id'], 'title': (run.get('plan') or {}).get('title') or run['request'][:100],
            'status': run['status'], 'project_name': names.get(run['project_id'], '项目'),
            'updated_at': run.get('updated_at', run.get('created_at')), 'reason': reason})
    today = datetime.now(timezone.utc).date()
    activity = {str(today - timedelta(days=days)): {'date': str(today - timedelta(days=days)), 'runs': 0, 'delivered': 0}
                for days in range(6, -1, -1)}
    delivered = {'ready_for_review', 'published'}
    for run in runs:
        day = str(run.get('created_at', ''))[:10]
        if day in activity:
            activity[day]['runs'] += 1
            activity[day]['delivered'] += int(run['status'] in delivered)
    return {
        'projects': len(projects), 'runs': len(runs),
        'active_runs': sum(r['status'] in ('received', 'planning', 'queued', 'running', 'verifying', 'publishing') for r in runs),
        'attention_runs': sum(r['status'] in attention_states for r in runs),
        'delivered_runs': sum(r['status'] in delivered for r in runs),
        'known_cost_usd': known, 'unknown_cost_runs': len(unknown_runs),
        'legacy_known_cost_usd': legacy_known,
        'recent_events': recent, 'attention': attention, 'model_usage': list(usage.values()),
        'activity': list(activity.values()), 'capabilities': len(capabilities),
        'engineering': engineering_overview(runs, capabilities, names),
    }


def evidence_bundle(store, rid):
    # SQLite read transaction gives run and event watermark the same snapshot.
    with store.connect() as db:
        db.execute('BEGIN')
        row = db.execute('SELECT data FROM runs WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        run = json.loads(row['data'])
        watermark = db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?', (rid,)).fetchone()[0]
    events = list(store.export_events(rid, through=watermark))
    return {'schema_version': 3, 'exported_at': now(), 'event_watermark': watermark,
            'event_count': len(events), 'run': run, 'events': events,
            'scope': '全部已记录事件；上游 SDK 已截断的内容保留原截断标记，内部推理不在导出范围内。'}


def evidence_markdown(bundle):
    run = bundle['run']
    title = (run.get('plan') or {}).get('title') or run['request'][:120]
    parts = [f'# {title}', f"运行：{run['id']}\n\n状态：{run['status']}\n\n导出时间：{bundle['exported_at']}",
             '## 需求\n\n' + run['request'],
             '## 计划、配置与交付\n\n````json\n' + json.dumps(run, ensure_ascii=False, indent=2) + '\n````',
             f"## 执行记录（{bundle['event_count']} 条）"]
    for event in bundle['events']:
        parts.append(f"### #{event['id']} · {event['type']} · {event['at']}\n\n````json\n" +
                     json.dumps(event['payload'], ensure_ascii=False, indent=2) + '\n````')
    parts.append(bundle['scope'])
    return '\n\n'.join(parts)


def router(store, service):
    api = APIRouter(prefix='/api/v3')
    policies = PolicyStore(store)

    @api.get('/environment')
    def environment():
        preview = getattr(service, 'preview_mode', False) is True
        return {'mode': 'preview' if preview else 'live',
                'label': '本地演练 · 使用脚本执行，不调用模型或外部服务' if preview else '工程运行环境'}

    @api.get('/projects/{pid}/policy')
    def get_policy(pid: str):
        return policies.get(pid)

    @api.put('/projects/{pid}/policy')
    def update_policy(pid: str, body: PolicyBody, request: Request):
        try:
            return policies.update(pid, body.model_dump(exclude={'revision'}), body.revision,
                                   request.state.user['username'])
        except ValueError as exc:
            from factory.control.store import Conflict
            if isinstance(exc, Conflict):
                raise
            raise HTTPException(422, str(exc)) from None

    @api.get('/overview')
    def get_overview():
        return overview(store)

    @api.post('/runs/{rid}/retry', status_code=201)
    def retry(rid: str, request: Request):
        return service.retry(rid, request.state.user['username'], actor_id=request.state.user['id'])

    @api.get('/runs/{rid}/export')
    def export(rid: str, format: Literal['json', 'markdown', 'zip'] = 'json'):
        bundle = evidence_bundle(store, rid)
        payload = json.dumps(bundle, ensure_ascii=False, indent=2)
        if format == 'json':
            return Response(payload, media_type='application/json',
                            headers={'Content-Disposition': f'attachment; filename="factory-{rid}.json"'})
        markdown = evidence_markdown(bundle)
        if format == 'markdown':
            return Response(markdown, media_type='text/markdown',
                            headers={'Content-Disposition': f'attachment; filename="factory-{rid}.md"'})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('run.json', payload)
            archive.writestr('report.md', markdown)
            archive.writestr('events.jsonl', '\n'.join(json.dumps(e, ensure_ascii=False) for e in bundle['events']) + '\n')
        return Response(buffer.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="factory-{rid}.zip"'})

    return api
