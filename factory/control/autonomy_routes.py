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
    apply_waiting: bool = Field(default=False, strict=True)


_OVERVIEW_EVENT_TYPES = (
    'user.message', 'plan.created', 'task.started', 'task.completed', 'task.failed',
    'execution.checkpoint', 'run.started', 'run.verified', 'verification.completed', 'check.completed',
    'github.publish_started', 'github.published', 'github.publish_failed', 'delivery.blocked',
    'run.failed', 'run.recovered', 'capability.harvest_failed', 'usage.recorded',
)


def _json_projection(value):
    if value is None:
        return None
    if isinstance(value, str) and value[:1] in {'[', '{'}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _events(store, run_ids):
    """Read only small lifecycle projections for the selected run IDs."""
    grouped = {}
    run_ids = tuple(run_ids)
    if not run_ids:
        return grouped
    marks = ','.join('?' for _ in run_ids)
    types = ','.join('?' for _ in _OVERVIEW_EVENT_TYPES)
    query = f'''SELECT id,run_id,task_id,type,at,
        json_extract(payload, '$.profile') AS profile,
        json_extract(payload, '$.model') AS model,
        json_extract(payload, '$.provider') AS provider,
        json_extract(payload, '$.cost_usd') AS cost_usd,
        json_extract(payload, '$.input_tokens') AS input_tokens,
        json_extract(payload, '$.output_tokens') AS output_tokens,
        json_extract(payload, '$.cached_input_tokens') AS cached_input_tokens,
        json_extract(payload, '$.cache_creation_input_tokens') AS cache_creation_input_tokens,
        json_extract(payload, '$.cache_usage_schema') AS cache_usage_schema,
        json_extract(payload, '$.attempts') AS attempts,
        json_extract(payload, '$.checks') AS checks,
        json_extract(payload, '$.message') AS message,
        json_extract(payload, '$.error') AS error,
        json_extract(payload, '$.status') AS status,
        json_extract(payload, '$.pr_url') AS pr_url
        FROM events WHERE run_id IN ({marks}) AND type IN ({types}) ORDER BY id'''
    with store.connect() as db:
        rows = db.execute(query, (*run_ids, *_OVERVIEW_EVENT_TYPES)).fetchall()
    for row in rows:
        event = {key: row[key] for key in ('id', 'run_id', 'task_id', 'type', 'at')}
        payload = {}
        for key in ('profile', 'model', 'provider', 'cost_usd', 'input_tokens', 'output_tokens',
                    'cached_input_tokens', 'cache_creation_input_tokens', 'attempts', 'checks',
                    'cache_usage_schema', 'message', 'error', 'status', 'pr_url'):
            value = _json_projection(row[key])
            if value is not None:
                payload[key] = value
        event['payload'] = payload
        event['version'] = 1
        grouped.setdefault(str(event['run_id']), []).append(event)
    return grouped


def _recent_events(store, run_ids):
    """Fetch complete payloads only for the bounded recent-events panel."""
    run_ids = tuple(run_ids)
    if not run_ids:
        return []
    marks = ','.join('?' for _ in run_ids)
    with store.connect() as db:
        rows = db.execute(
            f'SELECT * FROM events WHERE run_id IN ({marks}) ORDER BY id DESC LIMIT 30', run_ids,
        ).fetchall()
    result = []
    for row in rows:
        event = dict(row)
        event['payload'] = json.loads(event['payload'])
        event['version'] = 1
        result.append(event)
    return result


def _attention(run, events):
    artifacts = run.get('artifacts') if isinstance(run.get('artifacts'), dict) else {}
    run_events = events.get(str(run.get('id')), [])
    status = run.get('status')
    active_attention = status in {'needs_clarification', 'awaiting_approval', 'needs_human', 'failed'}
    has_needs_human = bool(artifacts.get('needs_human'))
    if not active_attention:
        return None
    if run.get('error'):
        reason = run['error']
    elif has_needs_human:
        reason = artifacts.get('needs_human') if isinstance(artifacts.get('needs_human'), str) else '运行需要人工处理'
    else:
        recovery_events = [event for event in run_events if event.get('type') == 'run.recovered']
        error_events = [event for event in run_events if event.get('type') in {
            'run.failed', 'task.failed', 'github.publish_failed', 'capability.harvest_failed',
        }]
        if status == 'needs_human' and recovery_events:
            payload = recovery_events[-1].get('payload') or {}
            reason = payload.get('message') or '服务恢复后等待人工核对'
        elif status == 'failed' and error_events:
            payload = error_events[-1].get('payload') or {}
            reason = payload.get('message') or payload.get('error') or '运行失败'
        else:
            triage = run.get('triage') or {}
            reason = '；'.join(triage.get('questions') or triage.get('reasons') or []) or '需要补充运行信息'
    questions = []
    seen_questions = set()
    for source in (run.get('triage') or {}, run.get('plan') or {}):
        for question in source.get('questions') or []:
            if isinstance(question, str) and question.strip() and question.strip() not in seen_questions:
                questions.append(question.strip())
                seen_questions.add(question.strip())
    return {
        'id': run['id'], 'project_id': run.get('project_id'), 'run_snapshot': run,
        'title': (run.get('plan') or {}).get('title') or str(run.get('request', ''))[:100],
        'status': run.get('status'), 'project_name': run.get('project_name', '项目'),
        'updated_at': run.get('updated_at', run.get('created_at')), 'reason': str(reason),
        'billing_incomplete': artifacts.get('billing_incomplete'),
        'questions': questions,
    }


def overview(store, project_id=None):
    from factory.control.capabilities import CapabilityStore
    projects = store.projects()
    project_by_id = {project['id']: project for project in projects}
    if project_id is not None and project_id not in project_by_id:
        raise KeyError(project_id)
    names = {project['id']: project['name'] for project in projects}
    from factory.control.engineering_overview import current_evidence
    all_runs = [{**run, 'progress': current_evidence(run)} for run in store.all_runs()]
    all_capabilities = CapabilityStore(store).list()
    run_by_id = {str(run['id']): run for run in all_runs}

    def scope_runs(pid):
        return [run for run in all_runs if pid is None or run.get('project_id') == pid]

    def scope_capabilities(pid, runs):
        if pid is None:
            return all_capabilities
        source_runs = {str(run['id']) for run in runs}
        return [capability for capability in all_capabilities
                if str(capability.get('source_run_id')) in source_runs]

    runs = scope_runs(project_id)
    capabilities = scope_capabilities(project_id, runs)
    scoped_run_ids = {str(run['id']) for run in runs}
    all_events = _events(store, scoped_run_ids)
    scoped_events = {rid: events for rid, events in all_events.items() if rid in scoped_run_ids}
    usage = {}
    known = 0.0
    unknown_runs = set()
    accounted = set()
    for rid, run_events in scoped_events.items():
        for event in run_events:
            if event.get('type') != 'usage.recorded':
                continue
            payload = event.get('payload') or {}
            key = (str(payload.get('profile') or 'unknown'), str(payload.get('model') or '未记录型号'),
                   str(payload.get('provider') or '未记录供应商'))
            item = usage.setdefault(key, {'profile': key[0], 'model': key[1], 'provider': key[2], 'calls': 0,
                                          'known_cost_usd': 0.0, 'unknown_cost_calls': 0,
                                          'input_tokens': 0, 'output_tokens': 0,
                                          'cached_input_tokens': 0,
                                          'cache_creation_input_tokens': 0,
                                          'token_usage_calls': 0, 'cache_usage_calls': 0})
            cost = valid_cost(payload.get('cost_usd'))
            item['calls'] += 1
            incoming = payload.get('input_tokens')
            outgoing = payload.get('output_tokens')
            cache_read = payload.get('cached_input_tokens')
            cache_created = payload.get('cache_creation_input_tokens')
            incoming = incoming if type(incoming) is int and incoming >= 0 else None
            outgoing = outgoing if type(outgoing) is int and outgoing >= 0 else None
            cache_read = cache_read if type(cache_read) is int and cache_read >= 0 else None
            cache_created = cache_created if type(cache_created) is int and cache_created >= 0 else None
            if incoming is not None or outgoing is not None:
                item['token_usage_calls'] += 1
                item['input_tokens'] += incoming or 0
                item['output_tokens'] += outgoing or 0
            # Older Claude events combined cache creation and cache reads in
            # cached_input_tokens. Only the explicit schema marker proves a
            # Claude row uses separate fields; a valid hit may have no cache
            # creation in that same call.
            claude_usage = str(payload.get('provider') or '').lower() in {'claude', 'anthropic'}
            separated = payload.get('cache_usage_schema') == 'separate_read_write_v1'
            if cache_read is not None and (not claude_usage or separated):
                item['cache_usage_calls'] += 1
                item['cached_input_tokens'] += cache_read
                item['cache_creation_input_tokens'] += cache_created or 0
            accounted.add(rid)
            if cost is None:
                item['unknown_cost_calls'] += 1
                unknown_runs.add(rid)
            elif math.isfinite(known + cost) and math.isfinite(item['known_cost_usd'] + cost):
                item['known_cost_usd'] += cost
                known += cost
            else:
                item['unknown_cost_calls'] += 1
                unknown_runs.add(rid)
    # Historical v2 totals remain visible without inventing missing call counts.
    legacy_known = 0.0
    for run in runs:
        if str(run['id']) not in accounted:
            artifacts = run.get('artifacts') or {}
            cost = valid_cost(artifacts.get('total_known_cost_usd', artifacts.get('known_cost_usd', artifacts.get('observed_cost_usd'))))
            if cost is not None and math.isfinite(known + cost):
                known += cost
                legacy_known += cost
            if artifacts.get('billing_incomplete'):
                unknown_runs.add(str(run['id']))
    recent = []
    for event in _recent_events(store, scoped_run_ids):
        rid = str(event['run_id'])
        run = run_by_id.get(rid, {})
        recent.append({**event, 'project_name': names.get(run.get('project_id'), '项目'),
                       'run_title': (run.get('plan') or {}).get('title') or str(run.get('request', ''))[:100]})
    attention = []
    for run in runs:
        enriched = {**run, 'project_name': names.get(run.get('project_id'), '项目')}
        item = _attention(enriched, scoped_events)
        if item:
            attention.append(item)
    attention.sort(key=lambda item: str(item.get('updated_at') or ''), reverse=True)
    attention_runs = len(attention)
    attention = attention[:10]
    today = datetime.now(timezone.utc).date()
    activity = {str(today - timedelta(days=days)): {'date': str(today - timedelta(days=days)), 'runs': 0, 'delivered': 0}
                for days in range(6, -1, -1)}
    delivered = {'ready_for_review', 'published'}
    for run in runs:
        day = str(run.get('created_at', ''))[:10]
        if day in activity:
            activity[day]['runs'] += 1
            activity[day]['delivered'] += int(run.get('status') in delivered)

    def engineering_for(pid, project_runs):
        project_caps = scope_capabilities(pid, project_runs)
        ids = {str(run['id']) for run in project_runs}
        return engineering_overview(project_runs, project_caps, names,
                                    {rid: scoped_events.get(rid, []) for rid in ids})

    project_summaries = []
    summary_projects = [project_by_id[project_id]] if project_id is not None else projects
    for project in summary_projects:
        project_runs = scope_runs(project['id'])
        project_attention = sum(_attention({**run, 'project_name': project['name']}, all_events) is not None
                                for run in project_runs)
        project_summaries.append({
            'id': project['id'], 'name': project['name'], 'repository': project.get('repository'),
            'budget_usd': project.get('budget_usd'), 'run_count': len(project_runs),
            'next_run': next(iter(sorted(project_runs, key=lambda run: (run.get('status') not in ('needs_human', 'needs_clarification', 'awaiting_approval'), run.get('status') not in ('received', 'planning', 'queued', 'running', 'verifying', 'ready_for_review', 'publishing'), project_runs.index(run)))), None),
            'active_runs': sum(run.get('status') in ('received', 'planning', 'queued', 'running', 'verifying', 'publishing')
                               for run in project_runs),
            'attention_runs': project_attention,
            'engineering': engineering_for(project['id'], project_runs),
        })
    return {
        'project_id': project_id, 'project_summaries': project_summaries,
        'snapshot_at': now(), 'run_snapshots': runs if project_id is not None else [],
        'projects': 1 if project_id is not None else len(projects), 'runs': len(runs),
        'active_runs': sum(r['status'] in ('received', 'planning', 'queued', 'running', 'verifying', 'publishing') for r in runs),
        'attention_runs': attention_runs,
        'delivered_runs': sum(r.get('status') in delivered for r in runs),
        'known_cost_usd': known, 'unknown_cost_runs': len(unknown_runs),
        'legacy_known_cost_usd': legacy_known, 'recent_events': recent,
        'attention': attention, 'model_usage': list(usage.values()), 'activity': list(activity.values()),
        'capabilities': len(capabilities), 'engineering': engineering_for(project_id, runs),
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
            policy, application = service.update_policy(
                pid, body.model_dump(exclude={'revision', 'apply_waiting'}), body.revision,
                request.state.user['username'], apply_waiting=body.apply_waiting,
            )
            if body.apply_waiting and body.mode != 'autonomous':
                application = {
                    'continued_run_ids': [],
                    'blocked': [{'run_id': '*', 'reason': '仅自主模式会接续待处理计划'}],
                }
            if body.apply_waiting:
                return {**policy, 'application': application}
            return policy
        except ValueError as exc:
            from factory.control.store import Conflict
            if isinstance(exc, Conflict):
                raise
            raise HTTPException(422, str(exc)) from None

    @api.get('/overview')
    def get_overview(project_id: str | None = None):
        try:
            return overview(store, project_id=project_id)
        except KeyError:
            raise HTTPException(404, '项目不存在') from None

    @api.get('/runs/{rid}/plans')
    def plan_versions(rid: str):
        store.get(rid)
        with store.connect() as db:
            rows = db.execute(
                "SELECT at,payload FROM events WHERE run_id=? AND type='plan.created' "
                "ORDER BY id DESC LIMIT 100", (rid,),
            ).fetchall()
        versions = []
        for row in rows:
            payload = json.loads(row['payload'])
            versions.append({
                'revision': payload.get('revision'),
                'summary': payload.get('summary'),
                'plan': payload.get('plan'),
                'at': row['at'],
            })
        versions.sort(key=lambda item: item.get('revision') if isinstance(item.get('revision'), int) else -1,
                      reverse=True)
        return {'versions': versions}

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
