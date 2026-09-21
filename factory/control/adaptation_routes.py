"""HTTP surface for the api-adaptation task port.

Prefix ``/api/v2/adaptation``.  The port (``AdaptationTasks``) is assembled by
``tasks_for(svc)`` and owns every state decision; this file only maps HTTP
verbs to port calls and translates the exceptions the port raises into status
codes, exactly the way ``maintenance_routes.py`` does for the maintenance port.

Availability: the port handed out by ``tasks_for`` is wrapped by the plugin
gate (``factory.control.plugins.gated``), so every call below is checked on
the service side. Nothing here reaches the run lifecycle directly the way
``maintenance_routes.approve`` does, so there is no method here that needs to
call ``tasks.gate.require`` by hand.

Exception mapping reuses the handlers already registered in ``app.py``:

* ``Conflict`` -> 409
* ``KeyError`` -> 404
* ``ValueError`` -> 422
* ``AuthError`` -> the status its constructor says (usually 403)

This module is not wired into ``app.py`` -- ``OWNERSHIP.md`` reserves that
file for the integrator. ``router(store, svc)`` returns an ``APIRouter`` ready
to be ``include_router``-ed once the integrator does so.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from factory.control.store import Conflict


def router(store, svc):
    from factory.control.api_adaptation import tasks_for

    tasks = tasks_for(svc)
    api = APIRouter(prefix='/api/v2/adaptation')

    def _actor(request: Request) -> dict:
        u = request.state.user
        return {'id': u['id'], 'username': u['username']}

    # -- availability ---------------------------------------------------

    @api.get('/availability')
    def availability(request: Request):
        """Whether this plugin currently accepts new adaptation work.

        Not the enforcement point -- every write below goes through the same
        service-side gate -- so a client that ignores this gains nothing but a
        refusal, exactly as ``maintenance_routes.availability`` documents.
        """
        _actor(request)
        return tasks.gate.availability.view(tasks.gate.plugin_id)

    # -- creation ---------------------------------------------------------

    @api.post('/tasks', status_code=201)
    async def create_task(request: Request):
        body = await request.json()
        try:
            return tasks.create(body, actor=_actor(request))
        except Conflict:
            # ``Conflict`` subclasses ``ValueError``; letting the clause below
            # catch it would report an availability refusal -- or any other
            # 409 the port raises -- as "malformed request".
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post('/tasks/{task_id}/revise', status_code=201)
    async def revise_task(task_id: str, request: Request):
        """Import a next contract version onto an existing task's lineage."""
        body = await request.json()
        try:
            return tasks.revise(task_id, body, actor=_actor(request))
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # -- reading ------------------------------------------------------------

    @api.get('/tasks')
    def list_tasks(request: Request, project_id: str):
        return {'tasks': tasks.list(actor=_actor(request), project_id=project_id),
                'availability': tasks.gate.availability.view(tasks.gate.plugin_id)}

    @api.get('/tasks/{task_id}')
    def get_task(task_id: str, request: Request):
        return tasks.get(task_id, actor=_actor(request))

    @api.get('/tasks/{task_id}/events')
    def task_events(task_id: str, request: Request, after: int = 0):
        events = tasks.events(task_id, actor=_actor(request), after=after)
        return {'events': events}

    # -- lifecycle ------------------------------------------------------

    @api.post('/tasks/{task_id}/cancel')
    def cancel(task_id: str, request: Request):
        return tasks.cancel(task_id, actor=_actor(request))

    # -- delivery -----------------------------------------------------------

    @api.get('/tasks/{task_id}/export')
    def export(task_id: str, request: Request):
        result = tasks.export(task_id, actor=_actor(request))
        artifacts = [{'name': a['name'], 'size': len(a['bytes'])}
                     for a in result['artifacts']]
        return {'receipt': result['receipt'], 'text': result['text'],
                'artifacts': artifacts}

    @api.get('/tasks/{task_id}/artifacts/{name:path}')
    def download_artifact(task_id: str, name: str, request: Request):
        # Resolve the name against the exported artifact list, never against
        # the filesystem, exactly as ``maintenance_routes.download_artifact``.
        result = tasks.export(task_id, actor=_actor(request))
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
