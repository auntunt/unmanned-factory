"""HTTP surface of the maintenance subsystem (contract ``maintenance-subsystem/1``).

Only maps verbs to ``MaintenanceSubsystem`` calls. The one route that does not
use a login session is ``POST /api/v2/maintenance/intake``: it authenticates an
intake-source token here, and the middleware lets it through exactly like the
signed GitHub webhook. Everything the token grants is the source's own project
list; a body field cannot widen it.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from factory.control.store import Conflict

INTAKE_PATH = '/api/v2/maintenance/intake'


class _Attachment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='', max_length=200)
    ref: str = Field(min_length=1, max_length=500)


class _RepoBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=120)
    branch: str | None = Field(default=None, max_length=200)
    credential_ref: str | None = Field(default=None, max_length=80)
    synthetic: bool = False


class _ManualBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20_000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)
    attachments: list[_Attachment] = Field(default_factory=list, max_length=20)


class _MachineBody(BaseModel):
    # Unknown fields are ignored rather than rejected: a caller's own ``source`` or
    # ``role`` field is simply not read, which is the point.
    model_config = ConfigDict(extra='ignore')
    project_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20_000)
    external_id: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=200)
    attachments: list[_Attachment] = Field(default_factory=list, max_length=20)


class _SourceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=60)
    project_ids: list[str] = Field(min_length=1, max_length=200)
    auto_dispatch: bool = False
    synthetic: bool = False


class _SyntheticBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    synthetic: bool


class _AdoptBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    adopt: list[str] = Field(min_length=1, max_length=20)


class _FeedbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=20_000)


def subsystem_for(svc, tasks, workspace_root=None):
    from factory.control.maintenance_subsystem import MaintenanceSubsystem
    return MaintenanceSubsystem(svc, tasks, workspace_root=workspace_root
                                or getattr(svc.targets, 'workspace_root', None))


def router(store, svc, tasks, workspace_root=None):
    core = subsystem_for(svc, tasks, workspace_root)
    api = APIRouter(prefix='/api/v2/maintenance')

    def _actor(request: Request) -> dict:
        u = request.state.user
        return {'id': u['id'], 'username': u['username']}

    def _admin(request: Request):
        if request.state.user.get('role') != 'admin':
            raise HTTPException(403, '此操作需要管理员权限')

    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # -- monitor ------------------------------------------------------------
    @api.get('/overview')
    def overview(request: Request, project_id: str | None = None):
        return core.overview(actor=_actor(request), project_id=project_id)

    @api.get('/graph')
    def graph(request: Request, project_id: str | None = None):
        return core.graph(actor=_actor(request), project_id=project_id)

    @api.get('/manifest')
    def manifest(request: Request):
        _actor(request)
        return core.manifest()

    # -- repositories -------------------------------------------------------
    @api.get('/repos')
    def repos(request: Request):
        return {'repos': core.repos(actor=_actor(request))}

    @api.post('/repos', status_code=201)
    def register(body: _RepoBody, request: Request):
        _admin(request)
        return _call(core.register_repo, source=body.source, name=body.name,
                     branch=body.branch, credential_ref=body.credential_ref, actor=_actor(request),
                     synthetic=body.synthetic)

    @api.post('/repos/{project_id}/synthetic')
    def mark_synthetic(project_id: str, body: _SyntheticBody, request: Request):
        _admin(request)
        return core.set_synthetic(project_id, body.synthetic, actor=_actor(request))

    @api.get('/repos/{project_id}')
    def repo(project_id: str, request: Request):
        return core.repo_view(project_id, actor=_actor(request), detail=True)

    @api.post('/repos/{project_id}/probe')
    def probe(project_id: str, request: Request):
        _admin(request)
        return core.probe(project_id, actor=_actor(request))

    @api.post('/repos/{project_id}/checks')
    def adopt(project_id: str, body: _AdoptBody, request: Request):
        _admin(request)
        return _call(core.adopt_checks, project_id, body.adopt, actor=_actor(request))

    # -- requirements -------------------------------------------------------
    @api.get('/requirements')
    def requirements(request: Request, project_id: str | None = None):
        return {'requirements': core.requirements(actor=_actor(request), project_id=project_id)}

    @api.post('/requirements', status_code=201)
    def submit(body: _ManualBody, request: Request):
        receipt, created = _call(
            core.submit, project_id=body.project_id, content=body.content,
            source_kind='manual', source_name=request.state.user['username'],
            actor=_actor(request), idempotency_key=body.idempotency_key,
            attachments=[a.model_dump() for a in body.attachments], auto_dispatch=True)
        if not created:
            from fastapi.responses import JSONResponse
            return JSONResponse(receipt, status_code=200)
        return receipt

    @api.post('/requirements/{requirement_id}/dispatch')
    def dispatch(requirement_id: str, request: Request):
        record = core.dispatch(requirement_id, actor=_actor(request))
        return core.receipt(record)

    @api.post('/intake', status_code=201)
    def machine_intake(body: _MachineBody, request: Request):
        header = request.headers.get('authorization', '')
        token = header[7:].strip() if header[:7].lower() == 'bearer ' else ''
        source = core.authenticate_source(token)
        if source is None:
            raise HTTPException(401, '接入令牌无效或已吊销')
        if body.project_id not in source['project_ids']:
            raise HTTPException(403, '这个接入来源无权向该项目提交需求')
        # The source acts with the authority of whoever created it, narrowed to its
        # own project list above; the username names the source, not a person.
        actor = {'id': source['created_by_id'], 'username': f"intake:{source['name']}"}
        receipt, created = _call(
            core.submit, project_id=body.project_id, content=body.content,
            source_kind='api', source_name=source['name'], actor=actor,
            external_id=body.external_id, title=body.title,
            attachments=[a.model_dump() for a in body.attachments],
            auto_dispatch=source['auto_dispatch'], synthetic=bool(source.get('synthetic')))
        if not created:
            from fastapi.responses import JSONResponse
            return JSONResponse(receipt, status_code=200)
        return receipt

    @api.get('/intake-sources')
    def sources(request: Request):
        _admin(request)
        return {'sources': core.sources()}

    @api.post('/intake-sources', status_code=201)
    def create_source(body: _SourceBody, request: Request):
        _admin(request)
        return _call(core.create_source, name=body.name, project_ids=body.project_ids,
                     auto_dispatch=body.auto_dispatch, actor=_actor(request),
                     synthetic=body.synthetic)

    @api.post('/intake-sources/{source_id}/revoke')
    def revoke(source_id: str, request: Request):
        _admin(request)
        return core.revoke_source(source_id)

    # -- task follow-up -----------------------------------------------------
    @api.post('/tasks/{task_id}/feedback', status_code=201)
    def feedback(task_id: str, body: _FeedbackBody, request: Request):
        return _call(core.feedback, task_id, body.content, actor=_actor(request))

    return api, core


def is_intake_request(path: str, method: str) -> bool:
    return method == 'POST' and hmac.compare_digest(path, INTAKE_PATH)
