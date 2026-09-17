"""Run submission, lifecycle and delivery HTTP endpoints; policy remains in Service."""
import hashlib
import json
import uuid
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import ConfigDict, Field
from factory.control.auth_routes import Body
from pydantic import BaseModel
from factory.control.store import Conflict, now
from factory.control import requirement_analysis, budget_resume
from factory.control.spec_refs import resolve as resolve_refs, fingerprint as refs_fingerprint

class GitHubPublishRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['existing', 'create', 'bound']
    repository: str | None = Field(default=None, max_length=240)
    name: str | None = Field(default=None, max_length=100)
    private: Literal[True] = True
    expected_project_revision: int = Field(ge=1)


class NewRun(Body):
    project_id: str
    request: str = Field(min_length=1, max_length=50_000)
    operation: str = Field(default="general", max_length=60)
    requirement_analysis: bool = Field(default=False, strict=True)
    interaction_mode: Literal['review', 'automatic'] = 'review'
    execute_deploy: bool = Field(default=False, strict=True)
    operation_fields: dict[str, str] = Field(default_factory=dict, max_length=8)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class Continuation(Body):
    answer: str = Field(default="", max_length=20000)
    revision: int = Field(ge=0)
    resume_count: int = Field(default=0, ge=0)


class BudgetContinuation(Continuation):
    # Analysis may pause before the first spec/plan revision exists.
    revision: int = Field(ge=0)


class Clarification(Body):
    answer: str = Field(min_length=1, max_length=50_000)


class Approval(Body):
    revision: int = Field(ge=1)


class FollowUp(Body):
    content: str = Field(min_length=1, max_length=50_000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


def _followup_status(store, rid):
    """Build the pending/applied follow-up list from durable events."""
    pending = {}
    for event in store.export_events(rid, kind='followup.pending'):
        p = event['payload']
        pending[p['id']] = {'id': p['id'], 'content': p.get('content', '')[:200],
                            'created_at': p.get('created_at', ''), 'applied': False}
    for event in store.export_events(rid, kind='followup.applied'):
        pid = event['payload'].get('pending_id')
        if pid in pending:
            pending[pid]['applied'] = True
    return list(pending.values())


def router(store, svc, operations):
    api = APIRouter()

    @api.get('/api/v2/runs')
    def runs(project_id: str | None = None):
        if project_id is None:
            return {'runs': store.runs()}
        store.project(project_id)
        return {'runs': [run for run in store.all_runs() if run.get('project_id') == project_id]}


    @api.post('/api/v2/runs', status_code=201)
    def new_run(body: NewRun, request: Request):
        project = store.project(body.project_id)
        try:
            compiled, fingerprint, preset = operations.compile(body.operation, body.request, body.operation_fields)
            if body.execute_deploy and body.operation != 'release':
                raise ValueError('执行部署仅适用于部署准备工作流')
            if body.execute_deploy:
                fingerprint = hashlib.sha256((fingerprint + '\0execute_deploy=true').encode()).hexdigest()
                compiled += '\n[用户明确授权：独立验收通过后，由平台执行绑定目标的预注册部署动作。]'
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if body.requirement_analysis:
            fingerprint = hashlib.sha256((fingerprint + '\0requirement_analysis=true').encode()).hexdigest()
        if body.interaction_mode == 'automatic':
            fingerprint = hashlib.sha256((fingerprint + '\0interaction_mode=automatic').encode()).hexdigest()
        spec_resolution = resolve_refs(project, body.request)
        fingerprint = refs_fingerprint(fingerprint, spec_resolution)
        key = (f"web:{request.state.user['id']}:{body.project_id}:{body.idempotency_key}"
               if body.idempotency_key else None)
        if body.operation == 'startup':
            compiled += svc.operations_automation.context(body.project_id)
        remote_targets = svc.targets.snapshot(body.project_id) if body.operation == 'release' else []
        run, created = store.create_run(body.project_id, compiled,
                                 source={'type': 'web', 'actor': request.state.user['username'],
                                         'actor_id': request.state.user['id'],
                                         'original_request': body.request, 'compiled_request_sha256': hashlib.sha256(compiled.encode()).hexdigest(),
                                         'requirement_analysis': body.requirement_analysis,
                                         'interaction_mode': body.interaction_mode,
                                         'operation': body.operation, 'operation_version': preset['version'],
                                         'execute_deploy': body.execute_deploy, 'remote_targets': remote_targets,
                                         'request_fingerprint': fingerprint, **spec_resolution}, delivery_id=key)
        if not created:
            if run['source'].get('request_fingerprint') != fingerprint:
                raise HTTPException(409, '这次提交已被接收；内容发生变化，请重新提交。')
            return run
        try:
            svc.start_plan(run['id'])
        except Exception as exc:
            svc._fail(run['id'], exc)
            raise
        return run


    @api.get('/api/v2/runs/{rid}')
    def get_run(rid: str):
        from factory.control.engineering_overview import current_evidence
        run = svc.remote.evidence(store.get(rid))
        followups = _followup_status(store, rid)
        return {**run, 'progress': current_evidence(run), 'followups': followups}


    @api.post('/api/v2/runs/{rid}/resume-budget')
    def resume_budget(rid: str, body: BudgetContinuation, request: Request):
        return budget_resume.resume(svc, rid, body.revision, body.resume_count, request.state.user['username'])

    @api.post('/api/v2/runs/{rid}/confirm-spec')
    def confirm_spec(rid: str, body: requirement_analysis.Confirmation, request: Request):
        try:
            return requirement_analysis.confirm(svc, rid, body, request.state.user['username'])
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post('/api/v2/runs/{rid}/clarify')
    def clarify(rid: str, body: Clarification, request: Request):
        return svc.clarify(rid, body.answer, request.state.user['username'])


    @api.post('/api/v2/runs/{rid}/continue')
    def continue_run(rid: str, body: Continuation, request: Request):
        return svc.continue_run(rid, body.answer, body.revision, body.resume_count, request.state.user['username'])


    @api.post('/api/v2/runs/{rid}/approve')
    def approve(rid: str, body: Approval, request: Request):
        return svc.approve(rid, body.revision, request.state.user['username'])


    @api.post('/api/v2/runs/{rid}/follow-up')
    def follow_up(rid: str, body: FollowUp, request: Request):
        from factory.control.store import ACTIVE
        content = body.content.strip()
        if not content:
            raise HTTPException(422, '请填写补充内容')
        actor, actor_id = request.state.user['username'], request.state.user['id']
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        # Serialize state selection with other lifecycle actions and make client
        # retries reuse the same persisted receipt, even after the run finishes.
        with svc.lock:
            run = store.get(rid)
            if body.idempotency_key:
                for event in store.export_events(rid, kind='followup.received'):
                    receipt = event['payload']
                    if receipt.get('actor_id') == actor_id and receipt.get('key') == body.idempotency_key:
                        if receipt.get('fingerprint') != fingerprint:
                            raise HTTPException(409, '这条补充已接收；内容变化后请重新提交')
                        return receipt['response']
            status = run['status']
            message = None
            if status in ('needs_clarification', 'awaiting_approval'):
                result = svc.clarify(rid, content, actor)
                response = {'recorded': True, 'applied': True, 'queued': False, 'run_id': rid, 'status': result['status']}
            elif status == 'needs_human':
                result = svc.continue_run(rid, content, run['revision'], run.get('resume_count', 0), actor)
                response = {'recorded': True, 'applied': True, 'queued': False, 'run_id': rid, 'status': result['status']}
            elif status in ACTIVE:
                pending_id = uuid.uuid4().hex
                message = {'text': content, 'followup': True, 'queued': True,
                    'applied': False, 'actor': actor, 'actor_id': actor_id,
                    'pending_id': pending_id}
                pending_event = {'id': pending_id, 'content': content,
                    'actor_id': actor_id, 'actor': actor,
                    'fingerprint': fingerprint, 'created_at': now()}
                response = {'recorded': True, 'applied': False, 'queued': True,
                    'message': '补充已记录，将在下一个安全节点自动并入任务。'}
            else:
                raise HTTPException(409, '任务已结束，请在下方开始新一轮修改。')
            # An active note and its retry receipt are one durable transaction.
            with store.connect() as db:
                if message is not None:
                    store._event(db, rid, 'user.message', message)
                    if pending_event is not None:
                        store._event(db, rid, 'followup.pending', pending_event)
                if body.idempotency_key:
                    store._event(db, rid, 'followup.received', {'actor_id': actor_id, 'key': body.idempotency_key,
                        'fingerprint': fingerprint, 'response': response})
            return response


    @api.post('/api/v2/runs/{rid}/cancel')
    def cancel(rid: str, request: Request):
        return svc.cancel(rid, request.state.user['username'])


    @api.post('/api/v2/runs/{rid}/discard')
    def discard(rid: str, request: Request):
        return svc.discard(rid, request.state.user['username'])


    @api.get('/api/v3/runs/{rid}/github-options')
    def github_options(rid: str, request: Request, page: int = 1):
        if request.state.user['role'] != 'admin':
            raise HTTPException(403, '此操作需要管理员权限')
        if not 1 <= page <= 100:
            raise HTTPException(422, '页码无效')
        try:
            return svc.github_options(rid, page=page)
        except (Conflict, KeyError):
            raise
        except Exception:
            raise HTTPException(502, '无法读取 GitHub 仓库，请检查凭据权限后重试。') from None


    @api.post('/api/v3/runs/{rid}/github-publish')
    def github_publish(rid: str, body: GitHubPublishRequest, request: Request):
        try:
            return svc.publish_github(rid, **body.model_dump(), actor=request.state.user['username'])
        except (Conflict, KeyError):
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception:
            message = (store.get(rid).get('artifacts') or {}).get('publish_error')
            raise HTTPException(502, message or 'GitHub 发布未完成；本地成果仍可查看和下载。请刷新仓库列表后重试。') from None


    @api.post('/api/v2/runs/{rid}/publish')
    def publish(rid: str):
        try:
            return svc.publish(rid)
        except (Conflict, KeyError):
            raise
        except Exception:
            message = (store.get(rid).get('artifacts') or {}).get('publish_error')
            raise HTTPException(502, message or 'GitHub 发布未完成；本地成果仍可查看和下载。') from None


    @api.get('/api/v2/runs/{rid}/events')
    def events(rid: str, after: int = 0):
        store.get(rid)
        rows = store.events(rid, max(0, after))
        return {'events': rows, 'cursor': rows[-1]['id'] if rows else max(0, after)}


    @api.get('/api/v2/runs/{rid}/conversation')
    def conversation(rid: str):
        store.get(rid)
        return {'messages': store.conversation(rid)}


    @api.get('/api/v2/runs/{rid}/export')
    def export(rid: str):
        run = store.get(rid)
        parts = [f"# 工程记录 {rid}", f"计划版本：{run['revision']} · 状态：{run['status']}"]
        for message in store.conversation(rid):
            parts.append(f"## {'用户' if message['role'] == 'user' else '系统'} · {message['at']}\n\n{message['content']}\n\n事件：{message['event_ids']}")
        parts.append('## 交付证据\n\n```json\n' + json.dumps(run['artifacts'], ensure_ascii=False, indent=2) + '\n```')
        if run.get('runtime_configuration'):
            parts.append('## 本次冻结的执行配置\n\n```json\n' +
                         json.dumps(run['runtime_configuration'], ensure_ascii=False, indent=2) + '\n```')
        if run.get('context'):
            parts.append('## 本次冻结的项目上下文\n\n```json\n' + json.dumps(run['context'], ensure_ascii=False, indent=2) + '\n```')
        return PlainTextResponse('\n\n'.join(parts), media_type='text/markdown',
            headers={'Content-Disposition': f'attachment; filename="factory-{rid}.md"'})

    return api
