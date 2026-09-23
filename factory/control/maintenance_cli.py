"""Standalone CLI for the webuddy maintenance subsystem, without starting the
webuddy web app.

Two ways to point this CLI at a data domain:

* ``--db PATH`` (legacy, still supported): an existing ``control.db`` file.
  Refuses to create one.
* ``--data-dir DIR`` (new): uses ``DIR/control.db``, falling back to
  ``FACTORY_CONTROL_DATA`` (default ``~/.factory/control``) when omitted.
  Also refuses to create a missing database for query commands -- except
  ``init``, whose entire job is to create it.

Two ways to get work executed:

* Point at the same data domain a running webuddy web service uses (same
  ``--data-dir`` / ``FACTORY_CONTROL_DATA``). The web service holds the
  executor lock and picks up whatever this CLI enqueues; this CLI never
  starts a scheduler for those commands.
* Run ``webuddy-maintenance runtime`` in a terminal with no web service
  attached to the same database. That command builds a *real* ``Service``,
  recovers, and acquires the worker lock, so it durably executes whatever
  is enqueued -- by this CLI or anything else pointed at the same DB --
  until it is stopped.

Calls ``MaintenanceSubsystem`` -- the same shared core the web routes use --
and ``issue_maintenance_webuddy.tasks_for`` for the legacy task-port
subcommands kept from the original CLI. The only CLI-specific ports are the
identity boundary, which is the local OS user, and (for read-only
subcommands) a dispatcher that refuses.

The identity boundary is the local OS user: only the Unix user running the
process (or an explicit --operator that matches it) is accepted.  This is
the honest boundary because the CLI is a local process, and the OS user is
the only identity the OS can attest to -- passing body.actor='admin' in a
web request is not the same as being the admin, but being the Unix user
running the CLI *is* being that user.  A remote service would resolve
identity from a session token; here the session is the OS process itself.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import time
from pathlib import Path

from factory.control.store import Conflict, Store


# ---------------------------------------------------------------------------
# Identity port: local operator boundary
# ---------------------------------------------------------------------------
class LocalOperatorIdentity:
    """Only the local OS user is accepted; arbitrary privileged actors are
    refused.

    Why this is the honest boundary: the CLI runs as a local subprocess.
    The only identity the OS can attest to is the Unix user.  Letting a
    caller name an arbitrary actor (e.g. --actor admin) would grant
    permissions the caller does not hold.  The webuddy web adapter resolves
    identity from an authenticated session; the CLI equivalent of that
    session is the OS process owner. Every project is permitted for the
    local OS user -- there is no per-project ACL to consult outside a web
    session, so the CLI's boundary is "you are this machine's operator",
    not "you are scoped to project X".
    """

    def __init__(self, allowed_username: str):
        self._allowed = allowed_username

    def require(self, actor, project_id) -> None:
        if not isinstance(actor, dict) or not actor.get('id'):
            raise Conflict(
                '维护任务需要一个已解析的受控身份，'
                'CLI 的受控身份来自本地操作系统用户')
        if actor.get('username') != self._allowed:
            raise Conflict(
                f"操作者 {actor.get('username')!r} 不是当前 OS 用户 "
                f"{self._allowed!r}，拒绝授权")


# ---------------------------------------------------------------------------
# Service construction (lazy, only when a command needs a dispatcher)
# ---------------------------------------------------------------------------
_service_cache: dict = {}


class _NeverRuns:
    """A runner that refuses to execute.  The CLI enqueues work for whoever
    holds the executor lock (a running webuddy web service, or
    ``webuddy-maintenance runtime``) to pick up; it does not run a model
    itself."""
    def run(self, *a, **kw):
        raise RuntimeError('CLI 不直接执行模型调用')


def _make_service(store):
    """Build a Service over the same database, exactly as
    tests/test_issue_maintenance_recovery.py does.

    The scheduler and _submit are patched so the CLI does not acquire
    the worker lock or start background threads.  create() goes through
    svc.start_plan -> _submit -> queue.enqueue, which durably records
    the run for pickup by whoever holds the executor lock.
    """
    if 'svc' in _service_cache:
        return _service_cache['svc']
    from factory.control.service import Service
    # No ``profiles`` argument: on a fresh database the first Service to open it
    # seeds the persisted runtime settings, and the runtime that later executes
    # the queue reads them. A placeholder here once became the real model config
    # (codex/"cli-enqueue") of every standalone install whose first command was
    # a CLI call. The environment is the only honest seed; this runner never
    # calls a model either way.
    svc = Service(store, runner=_NeverRuns())
    # Do not start the scheduler thread or acquire the worker lock.
    # The CLI is a single-shot invocation; it enqueues to the durable
    # queue and exits.  Whoever holds the executor lock picks the job up.
    svc._ensure_scheduler = lambda: None

    def _enqueue_only(fn, rid):
        phase = ('requirement_analysis' if fn == svc._analyze
                 else 'plan' if fn == svc._plan else 'execute')
        svc.queue.enqueue(rid, phase)

    svc._submit = _enqueue_only
    _service_cache['svc'] = svc
    return svc


# ---------------------------------------------------------------------------
# Port assembly
# ---------------------------------------------------------------------------
def _build_tasks(store, *, operator_username: str,
                 needs_dispatch: bool):
    """Wire the real adapter ports with the CLI's local-operator identity.

    This calls the *same* assembly the web surface calls
    (``issue_maintenance_webuddy.tasks_for``) rather than repeating it, so the
    plugin availability gate the web surface passes is not something the CLI
    could be built without.

    A Service is always constructed so cost accounting uses the same
    reconciliation logic the web surface uses (svc._usage).  When
    needs_dispatch is True, svc.start_plan is also wired as the dispatch
    function.  For read-only commands, dispatch raises if somehow called.
    """
    from factory.control.issue_maintenance_webuddy import tasks_for
    svc = _make_service(store)
    dispatch = None if needs_dispatch else (
        lambda rid: (_ for _ in ()).throw(Conflict('此命令不需要调度器')))
    return tasks_for(svc, identity=LocalOperatorIdentity(operator_username),
                     dispatch=dispatch)


def _build_subsystem(store, *, operator_username: str, needs_dispatch: bool,
                     workspace_root):
    from factory.control.maintenance_subsystem import MaintenanceSubsystem
    tasks = _build_tasks(store, operator_username=operator_username,
                         needs_dispatch=needs_dispatch)
    svc = _service_cache['svc']
    return MaintenanceSubsystem(svc, tasks, workspace_root=workspace_root,
                                identity=LocalOperatorIdentity(operator_username))


def _actor(username: str) -> dict:
    """Build the controlled actor dict from the verified local username."""
    return {'id': f'local:{username}', 'username': username}


# Commands that dispatch new work into the durable queue. These commands must
# print ``executor`` in their result so the caller knows whether anything will
# actually pick the work up.
_DISPATCH_COMMANDS = frozenset({
    'create', 'submit', 'dispatch', 'feedback', 'approve', 'answer',
})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='maintenance-cli',
        description='webuddy maintenance subsystem CLI (standalone, no webuddy web app required). '
                     'Installed as the webuddy-maintenance console script.',
    )
    p.add_argument('--db', default=None, metavar='PATH',
                   help='Existing control.db (refuses to create). Mutually '
                        'exclusive with --data-dir; --db wins if both are given.')
    p.add_argument('--data-dir', default=None, metavar='DIR',
                   help='Data directory containing control.db (default: '
                        '$FACTORY_CONTROL_DATA or ~/.factory/control). '
                        'Only "init" may create a missing database here.')
    p.add_argument('--workspace-root', default=None, metavar='DIR',
                   help='Root directory repositories must live under '
                        '(default: $FACTORY_WORKSPACE_ROOT or ~/projects)')
    p.add_argument('--operator', default=None, metavar='USER',
                   help='Operator username (must match OS user)')
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('init', help='Create the data directory and control.db')

    # -- repositories ---------------------------------------------------
    repo = sub.add_parser('repo', help='Repository (代码库) commands')
    repo_sub = repo.add_subparsers(dest='repo_command', required=True)
    ra = repo_sub.add_parser('add', help='Register a repository')
    ra.add_argument('--source', required=True,
                    help='Git URL or a directory on the execution host')
    ra.add_argument('--name', required=True)
    ra.add_argument('--branch', default=None)
    ra.add_argument('--synthetic', action='store_true',
                    help='Declare this a synthetic/demo repository (carried into tasks and receipts)')
    rsy = repo_sub.add_parser('mark-synthetic', help='Declare or withdraw the synthetic flag')
    rsy.add_argument('project_id')
    rsy.add_argument('--off', action='store_true', help='Withdraw the flag')
    repo_sub.add_parser('list', help='List registered repositories')
    rs = repo_sub.add_parser('show', help='Show one repository')
    rs.add_argument('project_id')
    rp = repo_sub.add_parser('probe', help='Re-analyze a repository')
    rp.add_argument('project_id')
    rc = repo_sub.add_parser('adopt-checks', help='Adopt suggested check commands')
    rc.add_argument('project_id')
    rc.add_argument('names', nargs='+', metavar='NAME')

    # -- requirements / intake -------------------------------------------
    sb = sub.add_parser('submit', help='Submit a requirement (Intake.submit)')
    sb.add_argument('--project', required=True)
    grp = sb.add_mutually_exclusive_group(required=True)
    grp.add_argument('--text', default=None)
    grp.add_argument('--file', default=None, metavar='PATH')
    sb.add_argument('--external-id', default=None)
    sb.add_argument('--idempotency-key', default=None)
    sb.add_argument('--no-dispatch', action='store_true',
                    help='Receive without dispatching; a human dispatches later')

    rq = sub.add_parser('requirements', help='List requirements')
    rq.add_argument('--project', default=None)

    ds = sub.add_parser('dispatch', help='Dispatch a received requirement')
    ds.add_argument('requirement_id')

    # -- monitor / graph ---------------------------------------------------
    ov = sub.add_parser('overview', help='Monitor overview')
    ov.add_argument('--project', default=None)
    gr = sub.add_parser('graph', help='Relationship graph projection')
    gr.add_argument('--project', default=None)

    # -- task actions --------------------------------------------------
    an = sub.add_parser('answer', help='Answer a clarification question')
    an.add_argument('task_id')
    an.add_argument('--text', required=True)

    ap = sub.add_parser('approve', help='Approve a plan awaiting approval')
    ap.add_argument('task_id')

    fb = sub.add_parser('feedback', help='Follow-up feedback on a finished task')
    fb.add_argument('task_id')
    fb.add_argument('--text', required=True)

    ac = sub.add_parser('actions', help='Actions currently permitted on a task')
    ac.add_argument('task_id')

    ev = sub.add_parser('events', help='Task execution events')
    ev.add_argument('task_id')
    ev.add_argument('--after', type=int, default=0,
                    help='Only events after this sequence number')
    ev.add_argument('--follow', action='store_true',
                    help='Keep polling and print each new event as NDJSON '
                         'until the task is delivered/failed/cancelled or Ctrl-C')
    ev.add_argument('--interval', type=float, default=2.0,
                    help='Polling interval in seconds for --follow (default 2)')

    # -- misc --------------------------------------------------------------
    sub.add_parser('manifest', help='Host manifest')
    sub.add_parser('version', help='CLI/contract version')

    # -- legacy task-port subcommands (kept backward compatible) -----------
    cr = sub.add_parser('create', help='Create a maintenance task (legacy task-port form)')
    cr.add_argument('--request-json', required=True, metavar='PATH',
                    help='JSON file with the maintenance request body')

    sub.add_parser('list',
                   help='List tasks for a project (legacy)').add_argument(
        '--project', required=True)

    show = sub.add_parser('show', help='Show a single task')
    show.add_argument('task_id')

    iv = sub.add_parser('intervene',
                        help='Supplement information on a waiting task')
    iv.add_argument('task_id')
    iv.add_argument('--text', required=True)

    res = sub.add_parser('resume', help='Resume a waiting task')
    res.add_argument('task_id')

    can = sub.add_parser('cancel', help='Cancel a task')
    can.add_argument('task_id')

    exp = sub.add_parser('export', help='Export delivery artifacts')
    exp.add_argument('task_id')
    exp.add_argument('--output', required=True, metavar='PATH',
                     help='Directory to write artifacts into')

    # -- standalone runtime --------------------------------------------
    rt = sub.add_parser('runtime',
                        help='Minimal standalone runtime: hold the executor '
                             'lock and really execute the durable queue')
    rt.add_argument('--once-idle-after', type=float, default=None, metavar='SECONDS',
                    help='Exit once the durable queue has had no pending/'
                         'running jobs for this many seconds (for scripted runs)')
    return p


def _resolve_db(args) -> Path:
    if args.db:
        return Path(args.db)
    data_dir = Path(args.data_dir or os.getenv('FACTORY_CONTROL_DATA', '~/.factory/control')).expanduser()
    return data_dir / 'control.db'


def _resolve_data_dir(args) -> Path:
    if args.db:
        return Path(args.db).parent
    return Path(args.data_dir or os.getenv('FACTORY_CONTROL_DATA', '~/.factory/control')).expanduser()


def _resolve_workspace_root(args) -> str:
    return args.workspace_root or os.getenv('FACTORY_WORKSPACE_ROOT', '~/projects')


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == 'init':
        return _init(args)
    # Static facts about this install: no data domain needed, none touched.
    if args.command in ('version', 'manifest'):
        from factory.control.maintenance_subsystem import (
            CONTRACT_VERSION, VERSION, MaintenanceSubsystem)
        print(json.dumps({'version': VERSION, 'contract_version': CONTRACT_VERSION}
                         if args.command == 'version' else MaintenanceSubsystem.manifest(),
                         ensure_ascii=False))
        return 0

    db_path = _resolve_db(args)
    if not db_path.is_file():
        print(json.dumps({'error': f'数据库不存在：{db_path}；拒绝创建（先运行 init）'},
                         ensure_ascii=False), file=sys.stderr)
        return 1

    # -- identity boundary --------------------------------------------------
    os_user = getpass.getuser()
    operator = args.operator or os_user
    if operator != os_user:
        # The CLI's identity IS the OS user.  --operator exists so a test
        # can name it explicitly, but it must match; anything else would
        # let a caller acquire permissions they do not hold.
        print(json.dumps({
            'error': f'--operator {operator!r} 与当前 OS 用户 '
                     f'{os_user!r} 不一致，拒绝授权',
        }, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.command == 'runtime':
        return _runtime_command(args, db_path)

    # ``show``/``list``/etc. (the original standalone CLI's subcommands) map
    # KeyError to exit 1, matching their long-standing behavior. The new
    # subsystem subcommands added here distinguish "not found" as exit 3,
    # per this CLI's documented exit-code contract. Both are honored by
    # branching on which family the command belongs to, rather than by
    # picking one mapping and breaking the other's callers.
    not_found_exit = 1 if args.command in _LEGACY_COMMANDS else 3
    try:
        store = Store(db_path)
        needs_dispatch = args.command in _DISPATCH_COMMANDS
        actor = _actor(operator)
        result = _dispatch(args, store, actor, operator, needs_dispatch)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except KeyError as exc:
        print(json.dumps({
            'error': str(exc),
            'error_type': type(exc).__name__,
        }, ensure_ascii=False), file=sys.stderr)
        return not_found_exit
    except (Conflict, ValueError) as exc:
        print(json.dumps({
            'error': str(exc),
            'error_type': type(exc).__name__,
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        # Never leak raw tracebacks that might contain credentials.
        print(json.dumps({
            'error': '操作失败，未报告成功',
            'error_type': type(exc).__name__,
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        svc = _service_cache.pop('svc', None)
        if svc is not None:
            svc.close()


def _init(args) -> int:
    data_dir = _resolve_data_dir(args)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / 'control.db'
        store = Store(db_path)  # creates schema
        # Touch the maintenance-subsystem tables too, so a fresh `init` is
        # immediately usable by `repo add` / `submit` without a second call.
        from factory.control.maintenance_subsystem import _ensure_tables
        _ensure_tables(store)
        print(json.dumps({'data_dir': str(data_dir), 'db': str(db_path)},
                         ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'error': f'初始化失败：{exc}', 'error_type': type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Legacy task-port subcommands
# ---------------------------------------------------------------------------
# ``events`` is not here: the subsystem handler covers the plain read and --follow.
_LEGACY_COMMANDS = frozenset({
    'create', 'list', 'show', 'intervene', 'resume', 'cancel', 'export',
})


def _dispatch(args, store, actor, operator, needs_dispatch) -> dict:
    cmd = args.command

    if cmd in _LEGACY_COMMANDS:
        tasks = _build_tasks(store, operator_username=operator,
                             needs_dispatch=needs_dispatch)
        return _dispatch_legacy(args, tasks, actor)

    workspace_root = _resolve_workspace_root(args)

    if cmd == 'repo':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=False,
                                     workspace_root=workspace_root)
        return _dispatch_repo(args, subsystem, actor)

    if cmd == 'submit':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=True,
                                     workspace_root=workspace_root)
        content = args.text
        if content is None:
            content = Path(args.file).read_text(encoding='utf-8')
        receipt, created = subsystem.submit(
            project_id=args.project, content=content, source_kind='cli',
            source_name=operator, actor=actor, external_id=args.external_id,
            idempotency_key=args.idempotency_key,
            auto_dispatch=not args.no_dispatch)
        return {**receipt, 'executor': subsystem.executor_status()}

    if cmd == 'requirements':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=False,
                                     workspace_root=workspace_root)
        return {'requirements': subsystem.requirements(actor=actor, project_id=args.project)}

    if cmd == 'dispatch':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=True,
                                     workspace_root=workspace_root)
        record = subsystem.dispatch(args.requirement_id, actor=actor)
        return {**subsystem.receipt(record), 'executor': subsystem.executor_status()}

    if cmd == 'overview':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=False,
                                     workspace_root=workspace_root)
        return subsystem.overview(actor=actor, project_id=args.project)

    if cmd == 'graph':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=False,
                                     workspace_root=workspace_root)
        return subsystem.graph(actor=actor, project_id=args.project)

    if cmd == 'answer':
        return _answer(store, args, actor, operator, workspace_root)

    if cmd == 'approve':
        return _approve(store, args, actor, operator, workspace_root)

    if cmd == 'feedback':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=True,
                                     workspace_root=workspace_root)
        view = subsystem.feedback(args.task_id, args.text, actor=actor)
        return {**view, 'executor': subsystem.executor_status()}

    if cmd == 'actions':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=False,
                                     workspace_root=workspace_root)
        view = subsystem.tasks.get(args.task_id, actor=actor)
        return {'task_id': args.task_id, 'status': view['status'],
                'actions': subsystem.task_actions(view)}

    if cmd == 'events':
        subsystem = _build_subsystem(store, operator_username=operator,
                                     needs_dispatch=False,
                                     workspace_root=workspace_root)
        return _events_command(subsystem, args, actor)

    if cmd == 'manifest':
        from factory.control.maintenance_subsystem import MaintenanceSubsystem
        return MaintenanceSubsystem.manifest()

    if cmd == 'version':
        from factory.control.maintenance_subsystem import VERSION, CONTRACT_VERSION
        return {'version': VERSION, 'contract_version': CONTRACT_VERSION}

    return {'error': f'未知命令 {cmd}'}


def _dispatch_repo(args, subsystem, actor) -> dict:
    rc = args.repo_command
    if rc == 'add':
        return subsystem.register_repo(
            source=args.source, name=args.name, actor=actor, branch=args.branch,
            background=False, synthetic=args.synthetic)
    if rc == 'mark-synthetic':
        return subsystem.set_synthetic(args.project_id, not args.off, actor=actor)
    if rc == 'list':
        return {'repos': subsystem.repos(actor=actor)}
    if rc == 'show':
        return subsystem.repo_view(args.project_id, actor=actor, detail=True)
    if rc == 'probe':
        return subsystem.probe(args.project_id, actor=actor, background=False)
    if rc == 'adopt-checks':
        return subsystem.adopt_checks(args.project_id, args.names, actor=actor)
    return {'error': f'未知子命令 repo {rc}'}


def _answer(store, args, actor, operator, workspace_root) -> dict:
    """``svc.clarify(execution_id, answer, username)``, gated exactly as the
    web route does: ``tasks.gate.require('continue')`` then ``tasks.get`` for
    authorization, before touching the run lifecycle."""
    subsystem = _build_subsystem(store, operator_username=operator,
                                 needs_dispatch=True, workspace_root=workspace_root)
    tasks = subsystem.tasks
    tasks.gate.require('continue')
    view = tasks.get(args.task_id, actor=actor)
    execution_id = view.get('execution_id')
    if not execution_id:
        raise Conflict('这个维护任务还没有绑定执行，无法回答问题')
    if view['status'] != 'waiting':
        raise Conflict(f"任务当前状态是 {view['status']!r}，不是等待回答")
    run = subsystem.store.get(execution_id)
    if run.get('status') != 'needs_clarification':
        raise Conflict(f"执行当前状态是 {run.get('status')!r}，不是等待回答问题")
    subsystem.svc.clarify(execution_id, args.text, actor['username'])
    view = tasks.get(args.task_id, actor=actor)
    return {**view, 'executor': subsystem.executor_status()}


def _approve(store, args, actor, operator, workspace_root) -> dict:
    """``svc.approve(execution_id, revision, username)``, gated exactly as the
    web route does, then a best-effort memory refine."""
    subsystem = _build_subsystem(store, operator_username=operator,
                                 needs_dispatch=True, workspace_root=workspace_root)
    tasks = subsystem.tasks
    tasks.gate.require('continue')
    view = tasks.get(args.task_id, actor=actor)
    execution_id = view.get('execution_id')
    if not execution_id:
        raise Conflict('这个维护任务还没有绑定执行，无法批准计划')
    run = subsystem.store.get(execution_id)
    subsystem.svc.approve(execution_id, run['revision'], actor['username'])
    try:
        tasks.port.execution.refine_memory_for_plan(execution_id)
    except Exception as exc:  # noqa: BLE001 - approval already happened; record, don't undo
        subsystem.store.append(execution_id, 'maintenance.memory_refine_failed',
                               {'message': str(exc)[:500]})
    view = tasks.get(args.task_id, actor=actor)
    return {**view, 'executor': subsystem.executor_status()}


_TERMINAL_STATUSES = frozenset({'delivered', 'failed', 'cancelled'})


def _events_command(subsystem, args, actor) -> dict:
    tasks = subsystem.tasks
    if not args.follow:
        return {'events': tasks.events(args.task_id, actor=actor, after=args.after)}
    after = args.after
    try:
        while True:
            events = tasks.events(args.task_id, actor=actor, after=after)
            for event in events:
                print(json.dumps(event, ensure_ascii=False, default=str))
                sys.stdout.flush()
                after = max(after, event.get('sequence', after))
            view = tasks.get(args.task_id, actor=actor)
            if view['status'] in _TERMINAL_STATUSES:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return {'followed': True, 'after': after}


def _dispatch_legacy(args, tasks, actor: dict) -> dict:
    cmd = args.command

    if cmd == 'create':
        request_path = Path(args.request_json)
        if not request_path.is_file():
            raise ValueError(f'请求文件不存在：{request_path}')
        request = json.loads(request_path.read_text(encoding='utf-8'))
        view = tasks.create(request, actor=actor)
        return {**view, 'executor': _executor_status_for(tasks)}

    if cmd == 'list':
        items = tasks.list(actor=actor, project_id=args.project)
        return {'tasks': items}

    if cmd == 'show':
        # Same enriched view the page reads (blocking reason, pending plan and
        # questions, allowed actions), not the bare port view.
        from factory.control.maintenance_routes import task_view
        from factory.control.maintenance_subsystem import MaintenanceSubsystem
        svc = _service_cache['svc']
        return task_view(svc.store, MaintenanceSubsystem(svc, tasks),
                         tasks.get(args.task_id, actor=actor))

    if cmd == 'events':
        evts = tasks.events(args.task_id, actor=actor, after=args.after)
        return {'events': evts}

    if cmd == 'intervene':
        return tasks.intervene(args.task_id, args.text, actor=actor)

    if cmd == 'resume':
        return tasks.resume(args.task_id, actor=actor)

    if cmd == 'cancel':
        return tasks.cancel(args.task_id, actor=actor)

    if cmd == 'export':
        exported = tasks.export(args.task_id, actor=actor)
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for artifact in exported['artifacts']:
            dest = out / artifact['name']
            dest.write_bytes(artifact['bytes'])
            written.append(str(dest))
        receipt = exported['receipt']
        text = exported['text']
        return {'receipt': receipt, 'text': text, 'written': written}

    return {'error': f'未知命令 {cmd}'}


def _executor_status_for(tasks) -> dict:
    svc = _service_cache.get('svc')
    if svc is None:
        return {'status': 'unknown', 'detail': '无法确定执行器状态'}
    from factory.control.maintenance_subsystem import executor_status
    return executor_status(svc, svc.store)


# ---------------------------------------------------------------------------
# Standalone runtime
# ---------------------------------------------------------------------------
def run_runtime(db_path, *, workspace_root=None, timeout_s=None, runner=None,
                stop_event=None, once_idle_after=None, print_fn=print) -> int:
    """The minimal standalone runtime.

    Builds a *real* ``Service`` (not the enqueue-only one the rest of this
    CLI uses), recovers, and acquires the worker lock via
    ``svc._ensure_scheduler()`` -- durably executing whatever is enqueued in
    the shared queue until stopped.

    ``runner`` and ``stop_event`` are injection points for tests: passing a
    fake runner avoids any real model call, and a pre-set ``stop_event``
    lets a test stop the runtime deterministically instead of racing a
    signal. Production use (``main``) leaves both as ``None``, which means
    "use the configured runtime" and "stop on SIGINT/SIGTERM".
    """
    from factory.control.service import Service
    from factory.control.autonomy import DurableQueue

    store = Store(db_path)
    timeout_s = timeout_s if timeout_s is not None else int(os.getenv('FACTORY_TASK_TIMEOUT', '14400'))
    svc = Service(store, runner=runner, timeout_s=timeout_s)
    svc.targets.workspace_root = Path(
        workspace_root or os.getenv('FACTORY_WORKSPACE_ROOT', '~/projects')).expanduser()
    try:
        svc.recover()
    except Exception as exc:
        print_fn(json.dumps({'error': f'恢复失败：{exc}', 'error_type': type(exc).__name__},
                            ensure_ascii=False), file=sys.stderr)
        svc.close()
        return 1
    try:
        svc._ensure_scheduler()
    except Conflict as exc:
        print_fn(json.dumps({'error': str(exc), 'error_type': 'Conflict'},
                            ensure_ascii=False), file=sys.stderr)
        svc.close()
        return 1

    from factory.control.maintenance_subsystem import executor_status
    print_fn(json.dumps({'runtime': 'ready', 'db': str(db_path), 'pid': os.getpid(),
                         'executor': executor_status(svc, store)}, ensure_ascii=False))
    sys.stdout.flush() if print_fn is print else None

    stop_event = stop_event if stop_event is not None else _signal_stop_event()
    idle_since = None
    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
            if once_idle_after is not None:
                queue = DurableQueue(store)
                busy = bool(queue.pending()) or bool(svc.active_jobs)
                if busy:
                    idle_since = None
                else:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= once_idle_after:
                        break
    finally:
        svc.close()
    print_fn(json.dumps({'runtime': 'stopped'}, ensure_ascii=False))
    return 0


def _signal_stop_event():
    import threading
    stop = threading.Event()

    def _handle(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    return stop


def _runtime_command(args, db_path) -> int:
    return run_runtime(db_path, workspace_root=_resolve_workspace_root(args),
                       once_idle_after=args.once_idle_after)


if __name__ == '__main__':
    raise SystemExit(main())
