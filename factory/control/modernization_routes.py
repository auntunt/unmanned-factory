"""HTTP surface for the legacy-modernization slice port.

Prefix ``/api/v2/modernization``. The port (``ModernizationPlans``) is
assembled by ``plans_for(svc)`` and owns every state decision; this file only
maps HTTP verbs to port calls and translates the exceptions the port raises
into status codes -- same contract as ``maintenance_routes.py``:

* ``Conflict`` -> 409
* ``KeyError`` -> 404
* ``ValueError`` -> 422
* ``AuthError`` -> the status its constructor says (usually 403)

Availability: the port handed out by ``plans_for`` is wrapped by the plugin
gate, so every call below is checked on the service side. ``approve`` is the
one exception, for the same reason it is one in ``maintenance_routes.py``: a
fresh run defaults to ``dag`` execution mode (this plugin's source type is not
in the small set of types eligible for continuous mode), so a slice sits at
``awaiting_approval`` until a human approves the plan -- and that decision
belongs to the run lifecycle, not to the port, so it checks the gate itself
before handing the approval to ``svc``.

This file does not register itself on ``app.py``; the integrator wires
``router(store, svc)`` in, the same way ``maintenance_router`` already is.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from factory.control.store import Conflict


class _TargetBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str
    target: dict
    source: str = ''
    paths: list[str] = Field(default_factory=list)
    commit_sha: str | None = None


class _ConfirmBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str
    dimension: str


def router(store, svc, *, availability=None, dispatch=None):
    """``availability``/``dispatch`` let a test substitute a fake gate or a fake
    executor dispatch without a second production code path existing -- the
    real wiring (the integrator's ``app.py``) calls ``router(store, svc)`` with
    both left at their defaults, which resolve to the live plugin-availability
    table and ``svc.start_plan`` respectively, exactly like ``maintenance_routes``.
    """
    from factory.control.legacy_modernization import plans_for

    plans = plans_for(svc, availability=availability, dispatch=dispatch)
    api = APIRouter(prefix='/api/v2/modernization')

    def _actor(request: Request) -> dict:
        u = request.state.user
        return {'id': u['id'], 'username': u['username']}

    # -- method / agreement ---------------------------------------------

    @api.get('/agreement')
    def agreement(request: Request):
        """The method version a new slice will be bound to.

        Read from the installed pack rather than typed by the caller, so the
        receipt records the version that actually applies.
        """
        _actor(request)
        from factory.control import agent_packs
        from factory.control.legacy_modernization import SCHEMA_VERSION
        pack = next((p for p in agent_packs.catalog()
                     if p['id'] == 'legacy-modernization'), None)
        if pack is None:
            raise HTTPException(503, '没有安装信创化改造方法包，暂时不能创建改造切片')
        return {'revision': f"v{SCHEMA_VERSION}",
                'skill_version': f"{pack['id']}@{pack['version']}",
                'pack': {'id': pack['id'], 'name': pack['name'],
                         'version': pack['version'],
                         'validation_status': pack['validation_status']}}

    @api.get('/availability')
    def availability(request: Request):
        _actor(request)
        return plans.gate.availability.view(plans.gate.plugin_id)

    # -- target / project memory -----------------------------------------

    @api.post('/targets', status_code=201)
    def register_target(body: _TargetBody, request: Request):
        actor = _actor(request)
        try:
            entries = plans.register_target(
                body.project_id, body.target, actor=actor, source=body.source,
                paths=body.paths, commit_sha=body.commit_sha)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {'entries': entries}

    @api.post('/targets/confirm')
    def confirm_target(body: _ConfirmBody, request: Request):
        actor = _actor(request)
        try:
            entry = plans.confirm_dimension(body.project_id, body.dimension, actor=actor)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return entry

    @api.get('/targets')
    def targets(request: Request, project_id: str):
        return {'dimensions': plans.dimensions(project_id, actor=_actor(request))}

    @api.get('/locate')
    def locate(request: Request, project_id: str, q: str, limit: int = 10):
        try:
            return plans.locate(project_id, q, actor=_actor(request), limit=limit)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get('/disposal-plan')
    def disposal_plan(request: Request, project_id: str):
        return {'plan': plans.disposal_plan(project_id, actor=_actor(request))}

    # -- slice creation ---------------------------------------------------

    @api.post('/slices', status_code=201)
    async def create_slice(request: Request):
        body = await request.json()
        try:
            return plans.create_slice(body, actor=_actor(request))
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post('/slices/{slice_id}/revise', status_code=201)
    async def revise_slice(slice_id: str, request: Request):
        body = await request.json()
        try:
            return plans.revise_slice(slice_id, body, actor=_actor(request))
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # -- reading ------------------------------------------------------------

    @api.get('/slices')
    def list_slices(request: Request, project_id: str):
        return {'slices': plans.list(actor=_actor(request), project_id=project_id),
                'availability': plans.gate.availability.view(plans.gate.plugin_id)}

    @api.get('/slices/{slice_id}')
    def get_slice(slice_id: str, request: Request):
        return plans.get(slice_id, actor=_actor(request))

    @api.get('/slices/{slice_id}/events')
    def slice_events(slice_id: str, request: Request, after: int = 0):
        events = plans.events(slice_id, actor=_actor(request), after=after)
        return {'events': events}

    # -- intervention -------------------------------------------------------

    @api.post('/slices/{slice_id}/approve')
    def approve(slice_id: str, request: Request):
        actor = _actor(request)
        plans.gate.require('continue')
        view = plans.get(slice_id, actor=actor)
        execution_id = view['execution_id']
        if not execution_id:
            raise HTTPException(409, '这个改造切片还没有绑定执行，无法批准计划')
        run = store.get(execution_id)
        svc.approve(execution_id, run['revision'], actor['username'])
        return get_slice(slice_id, request)

    @api.post('/slices/{slice_id}/resume')
    def resume(slice_id: str, request: Request):
        return plans.resume(slice_id, actor=_actor(request))

    @api.post('/slices/{slice_id}/cancel')
    def cancel(slice_id: str, request: Request):
        return plans.cancel(slice_id, actor=_actor(request))

    # -- delivery -----------------------------------------------------------

    @api.get('/slices/{slice_id}/export')
    def export(slice_id: str, request: Request):
        result = plans.export(slice_id, actor=_actor(request))
        artifacts = [{'name': a['name'], 'size': len(a['bytes'])}
                     for a in result['artifacts']]
        return {'receipt': result['receipt'], 'text': result['text'],
                'artifacts': artifacts}

    @api.get('/slices/{slice_id}/artifacts/{name:path}')
    def download_artifact(slice_id: str, name: str, request: Request):
        result = plans.export(slice_id, actor=_actor(request))
        match = next((a for a in result['artifacts'] if a['name'] == name), None)
        if match is None:
            raise HTTPException(404, '构建物不存在')
        return Response(
            match['bytes'],
            media_type='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{name}"',
                'X-Content-Type-Options': 'nosniff',
            })

    return api
