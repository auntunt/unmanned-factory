"""HTTP surface for the api-adaptation task port.

Prefix ``/api/v2/adaptation``.  The port (``AdaptationTasks``) is assembled by
``tasks_for(svc)`` and owns every state decision; this file only maps HTTP
verbs to port calls and translates the exceptions the port raises into status
codes, exactly the way ``maintenance_routes.py`` does for the maintenance port.

Availability: the port handed out by ``tasks_for`` is wrapped by the plugin
gate (``factory.control.plugins.gated``), so every call below is checked on
the service side. ``approve`` is the exception -- it hands its decision to the
run lifecycle rather than to the port -- and therefore calls
``tasks.gate.require('continue')`` by hand, exactly as ``maintenance_routes``
does.

``approve`` exists because a run defaults to ``dag`` execution and stops at
``awaiting_approval`` until a human accepts the plan. Without it an adaptation
task created from the page sat at that gate forever: the port's own methods
never touch approval, so nothing in this module could move it. That was found
by driving the real service, not by the unit tests -- those injected a
dispatcher that skipped the approval gate entirely.

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

from factory.control.api_adaptation import PLUGIN_ID, SCHEMA_VERSION
from factory.control.store import Conflict


def _pending_plan(store, execution_id) -> dict | None:
    """The plan awaiting a human decision, or nothing.

    Same shape ``maintenance_routes`` renders, so one page pattern covers both
    surfaces: titles, the files each task touches and the checks it will run.
    """
    if not execution_id:
        return None
    try:
        run = store.get(execution_id)
    except KeyError:
        return None
    plan = run.get('plan')
    if run.get('status') != 'awaiting_approval' or not plan:
        return None
    return {
        'revision': run['revision'],
        'title': plan.get('title') or '',
        'summary': plan.get('summary') or '',
        'questions': list(plan.get('questions') or []),
        'tasks': [{'id': t.get('id'), 'title': t.get('title') or '',
                   'paths': list(t.get('paths') or []),
                   'checks': list(t.get('checks') or []),
                   'risk': t.get('risk') or ''}
                  for t in (plan.get('tasks') or [])],
    }


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

    def _resolved_agreement():
        """The method version a new task binds to, read from the installed pack.

        Same rule the maintenance surface already follows: the version comes
        from what is actually installed, never from the request body. A person
        typing a version into a form is a guess, and a receipt that quotes that
        guess is a receipt about nothing -- so whatever the client sends here is
        replaced, not merged.
        """
        from factory.control import agent_packs
        pack = next((p for p in agent_packs.catalog()
                     if p['id'] == PLUGIN_ID), None)
        if pack is None:
            raise HTTPException(503, '没有安装接口适配方法包，暂时不能创建适配任务')
        return {'revision': f'v{SCHEMA_VERSION}',
                'skill_version': f"{pack['id']}@{pack['version']}",
                'pack': {'id': pack['id'], 'name': pack['name'],
                         'version': pack['version'],
                         'validation_status': pack['validation_status']}}

    @api.get('/agreement')
    def agreement(request: Request):
        _actor(request)
        return _resolved_agreement()

    def _with_server_agreement(body):
        resolved = _resolved_agreement()
        if not isinstance(body, dict):
            raise HTTPException(422, '请求体必须是一个对象')
        return {**body, 'agreement': {'revision': resolved['revision'],
                                      'skill_version': resolved['skill_version']}}

    @api.post('/tasks', status_code=201)
    async def create_task(request: Request):
        body = _with_server_agreement(await request.json())
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
        body = _with_server_agreement(await request.json())
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
        view = tasks.get(task_id, actor=_actor(request))
        # The plan a human is being asked to accept travels with the task:
        # approving something the page cannot show is not a decision.
        view['pending_plan'] = _pending_plan(store, view['execution_id'])
        if view['pending_plan'] and not (view.get('blocking_reason') or {}).get('kind'):
            view['blocking_reason'] = {
                'kind': 'approval.required',
                'message': '计划已就绪，等待人工批准后才会开始改动',
                'event': None}
        return view

    @api.get('/tasks/{task_id}/events')
    def task_events(task_id: str, request: Request, after: int = 0):
        events = tasks.events(task_id, actor=_actor(request), after=after)
        return {'events': events}

    @api.post('/tasks/{task_id}/approve')
    def approve(task_id: str, request: Request):
        """Accept the plan this task is waiting on, through the existing gate.

        The decision belongs to the run lifecycle, so this only carries it
        there -- after the same plugin-availability check and the same project
        authorization every other call goes through. Approving is what makes
        the executor start changing files, so a stopped plugin that still
        accepted approvals would be stopped in name only.
        """
        actor = _actor(request)
        tasks.gate.require('continue')
        view = tasks.get(task_id, actor=actor)
        execution_id = view['execution_id']
        if not execution_id:
            raise HTTPException(409, '这个适配任务还没有绑定执行，无法批准计划')
        run = store.get(execution_id)
        svc.approve(execution_id, run['revision'], actor['username'])
        return get_task(task_id, request)

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
