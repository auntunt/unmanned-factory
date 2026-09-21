"""HTTP surface for the maintenance-task port.

Prefix ``/api/v2/maintenance``.  The port (``MaintenanceTasks``) is assembled by
``tasks_for(svc)`` and owns every state decision; this file only maps HTTP verbs
to port calls and translates the exceptions the port raises into status codes.

Availability: the port handed out by ``tasks_for`` is wrapped by the plugin
gate, so every call below is checked on the service side.  ``approve`` is the
exception that proves it -- it hands its decision to the run lifecycle rather
than to the port, and therefore calls ``tasks.gate.require`` itself.

Exception mapping reuses the handlers already registered in ``app.py``:

* ``Conflict`` -> 409
* ``KeyError`` -> 404
* ``ValueError`` -> 422
* ``AuthError`` -> the status its constructor says (usually 403)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from factory.control.store import Conflict


class _FollowUpBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=50_000)


def _supplements(store, execution_id):
    """The durable pending/applied/expired supplements, or none when unbound.

    Reuses ``run_routes._followup_status`` so this surface and the run surface
    read the same events rather than each inventing a supplement state.
    """
    if not execution_id:
        return []
    from factory.control.run_routes import _followup_status
    return _followup_status(store, execution_id)


def _resumable(store, execution_id, status) -> bool:
    """Whether 「继续执行」 would actually be accepted right now.

    Waiting is not the same as resumable: a run that stopped before it had a
    plan sits at a human gate the resume path refuses. Offering the button
    anyway turns a real state into a click that always fails.
    """
    if status != 'waiting' or not execution_id:
        return False
    try:
        run = store.get(execution_id)
    except KeyError:
        return False
    return run.get('status') == 'needs_human' and bool(run.get('plan'))


def _pending_plan(store, execution_id) -> dict | None:
    """The plan a human is being asked to approve, or nothing.

    Approving something the page cannot show is not a decision, so the titles,
    the files each task touches and the checks it will run travel with the task.
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


def _state(supplement) -> str:
    if supplement['applied']:
        return 'applied'
    return 'expired' if supplement['expired'] else 'pending'


def _pending_questions(store, execution_id) -> list:
    """The questions a human is being asked, or none.

    A model that asks before acting is doing the right thing; a surface that
    cannot show the question turns that into a dead end. Same shape the
    adaptation surface already renders, so one page component covers all three.
    """
    if not execution_id:
        return []
    try:
        run = store.get(execution_id)
    except KeyError:
        return []
    if run.get('status') != 'needs_clarification':
        return []
    return list((run.get('plan') or {}).get('questions') or [])


def router(store, svc):
    from factory.control.issue_maintenance_webuddy import tasks_for

    tasks = tasks_for(svc)
    api = APIRouter(prefix='/api/v2/maintenance')

    def _actor(request: Request) -> dict:
        u = request.state.user
        return {'id': u['id'], 'username': u['username']}

    # -- creation -----------------------------------------------------------

    @api.get('/agreement')
    def agreement(request: Request):
        """The method version a new task will be bound to.

        Read from the installed pack rather than typed by the caller, so the
        receipt records the version that actually applies instead of whatever a
        form happened to send.
        """
        _actor(request)
        from factory.control import agent_packs
        from factory.control.issue_maintenance import SCHEMA_VERSION
        pack = next((p for p in agent_packs.catalog()
                     if p['id'] == 'issue-maintenance'), None)
        if pack is None:
            raise HTTPException(503, '没有安装 Issue 维护方法包，暂时不能创建维护任务')
        return {'revision': f"v{SCHEMA_VERSION}",
                'skill_version': f"{pack['id']}@{pack['version']}",
                'pack': {'id': pack['id'], 'name': pack['name'],
                         'version': pack['version'],
                         'validation_status': pack['validation_status']}}

    @api.post('/tasks', status_code=201)
    async def create_task(request: Request):
        body = await request.json()
        try:
            return tasks.create(body, actor=_actor(request))
        except Conflict:
            # ``Conflict`` subclasses ``ValueError``. Letting the clause below
            # catch it would report an availability refusal -- or any other
            # 409 the port raises -- as "malformed request", which points the
            # caller at their own body instead of at the stopped plugin.
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # -- reading ------------------------------------------------------------

    @api.get('/availability')
    def availability(request: Request):
        """Whether this plugin currently accepts new maintenance work.

        The page reads this to decide what to offer. It is not the enforcement
        point -- every write below goes through the same service-side gate -- so
        a client that ignores it gains nothing but a refusal.
        """
        _actor(request)
        return tasks.gate.availability.view(tasks.gate.plugin_id)

    @api.get('/tasks')
    def list_tasks(request: Request, project_id: str):
        return {'tasks': tasks.list(actor=_actor(request), project_id=project_id),
                'availability': tasks.gate.availability.view(tasks.gate.plugin_id)}

    @api.get('/tasks/{task_id}')
    def get_task(task_id: str, request: Request):
        view = tasks.get(task_id, actor=_actor(request))
        # Supplements are part of what the task现场 must explain, and they have
        # to survive a refresh, so they travel with the task rather than only in
        # the reply to the POST that created them.
        view['supplements'] = [{**s, 'state': _state(s)}
                               for s in _supplements(store, view['execution_id'])]
        view['resumable'] = _resumable(store, view['execution_id'], view['status'])
        view['pending_plan'] = _pending_plan(store, view['execution_id'])
        view['pending_questions'] = _pending_questions(store, view['execution_id'])
        if view['pending_questions'] and (view['blocking_reason'] or {}).get('kind') in (None, 'unknown'):
            # Answering is not resuming: a run stopped on a question needs the
            # answer, and 「继续执行」 would only push it back at the same gate.
            view['blocking_reason'] = {
                'kind': 'clarification.requested',
                'message': '模型在动手前提出了需要确认的问题，回答后才会继续',
                'event': None}
        if view['pending_plan'] and (view['blocking_reason'] or {}).get('kind') == 'unknown':
            # The port only cites events it already knows as blocking, and a plan
            # awaiting approval is not one of them. Saying "no citable reason"
            # while the plan sits right there teaches the reader to ignore the
            # field, so the gate names itself.
            view['blocking_reason'] = {
                'kind': 'approval.required',
                'message': '计划已就绪，等待人工批准后才会开始改动',
                'event': None}
        return view

    @api.get('/tasks/{task_id}/events')
    def task_events(task_id: str, request: Request, after: int = 0):
        events = tasks.events(task_id, actor=_actor(request), after=after)
        return {'events': events}

    # -- intervention -------------------------------------------------------

    @api.post('/tasks/{task_id}/follow-up')
    def follow_up(task_id: str, body: _FollowUpBody, request: Request):
        view = tasks.intervene(task_id, body.content, actor=_actor(request))
        # The port records a supplement at a human gate; the run applies it when
        # it resumes. Reporting "applied" here would be a receipt for something
        # that has not happened, so the answer is read back from the events.
        supplements = _supplements(store, view['execution_id'])
        # _followup_status truncates the stored content the same way for every
        # supplement, so this compares like with like.
        mine = next((s for s in reversed(supplements)
                     if s['content'] == body.content[:200]), None)
        state = _state(mine) if mine else 'pending'
        return {'recorded': True, 'applied': state == 'applied', 'state': state,
                'supplements': [{**s, 'state': _state(s)} for s in supplements],
                'task_id': task_id, 'status': view['status']}

    @api.post('/tasks/{task_id}/clarify')
    async def clarify(task_id: str, request: Request):
        """Answer the questions this task is waiting on, through the existing gate.

        Carries a human answer to the run lifecycle rather than to the port, so
        it performs the same plugin-availability check by hand that ``approve``
        does -- answering is what lets the executor start, and a stopped plugin
        must not accept one. Project authorization comes from ``tasks.get``
        below, exactly as every other call here.
        """
        actor = _actor(request)
        tasks.gate.require('continue')
        body = await request.json()
        answer = (body or {}).get('answer')
        if not isinstance(answer, str) or not answer.strip():
            raise HTTPException(422, '回答不能为空')
        view = tasks.get(task_id, actor=actor)
        execution_id = view['execution_id']
        if not execution_id:
            raise HTTPException(409, '这个维护任务还没有绑定执行，无法回答问题')
        svc.clarify(execution_id, answer.strip(), actor['username'])
        return get_task(task_id, request)

    @api.post('/tasks/{task_id}/approve')
    def approve(task_id: str, request: Request):
        """Approve the plan this task is waiting on, through the existing gate.

        The decision belongs to the run lifecycle; this only carries it there
        after the same project authorization every other task call goes through.
        """
        actor = _actor(request)
        # This is the one action that reaches the run lifecycle directly instead
        # of through the gated port, so it performs the same check by hand.
        # Approving a plan is what makes the executor start changing files: a
        # stopped plugin that still accepted approvals would be stopped in name
        # only.
        tasks.gate.require('continue')
        view = tasks.get(task_id, actor=actor)
        execution_id = view['execution_id']
        if not execution_id:
            raise HTTPException(409, '这个维护任务还没有绑定执行，无法批准计划')
        run = store.get(execution_id)
        svc.approve(execution_id, run['revision'], actor['username'])
        # The plan names the files, so memory can finally be narrowed to them.
        # Binding requirements already travelled whole at dispatch; this records
        # which of them the plan actually touches and fills in any context that
        # was only summarised. A failure here must not undo an approval that
        # already happened, so it is recorded and not raised.
        try:
            tasks.port.execution.refine_memory_for_plan(execution_id)
        except Exception as exc:  # noqa: BLE001
            store.append(execution_id, 'maintenance.memory_refine_failed',
                         {'message': str(exc)[:500]})
        return get_task(task_id, request)

    @api.post('/tasks/{task_id}/resume')
    def resume(task_id: str, request: Request):
        return tasks.resume(task_id, actor=_actor(request))

    @api.post('/tasks/{task_id}/cancel')
    def cancel(task_id: str, request: Request):
        return tasks.cancel(task_id, actor=_actor(request))

    # -- delivery -----------------------------------------------------------

    @api.get('/tasks/{task_id}/export')
    def export(task_id: str, request: Request):
        result = tasks.export(task_id, actor=_actor(request))
        # Strip raw bytes; the artifacts endpoint serves them.
        artifacts = [{'name': a['name'], 'size': len(a['bytes'])}
                     for a in result['artifacts']]
        return {'receipt': result['receipt'], 'text': result['text'],
                'artifacts': artifacts}

    @api.get('/tasks/{task_id}/artifacts/{name:path}')
    def download_artifact(task_id: str, name: str, request: Request):
        # Resolve the name against the exported artifact list, never against
        # the filesystem.  A name containing path-traversal characters is
        # harmless because it is only compared to the list, never opened.
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
