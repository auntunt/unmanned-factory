"""Standalone CLI for issue maintenance, without starting the webuddy web app.

Calls ``issue_maintenance_webuddy.tasks_for`` -- the same assembly the web
surface calls, including the plugin availability gate -- rather than building
its own port.  The only CLI-specific ports are the identity boundary, which is
the local OS user, and (for read-only subcommands) a dispatcher that refuses.

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
import sys
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
    session is the OS process owner.
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
# Service construction (lazy, only when create needs a dispatcher)
# ---------------------------------------------------------------------------
_service_cache: dict = {}


class _NeverRuns:
    """A runner that refuses to execute.  The CLI enqueues work for the
    running webuddy service to pick up; it does not run a model itself."""
    def run(self, *a, **kw):
        raise RuntimeError('CLI 不直接执行模型调用')


def _make_service(store):
    """Build a Service over the same database, exactly as
    tests/test_issue_maintenance_recovery.py does.

    The scheduler and _submit are patched so the CLI does not acquire
    the worker lock or start background threads.  create() goes through
    svc.start_plan -> _submit -> queue.enqueue, which durably records
    the run for pickup by the running service.
    """
    if 'svc' in _service_cache:
        return _service_cache['svc']
    from factory.control.service import Service
    svc = Service(
        store, runner=_NeverRuns(),
        profiles={role: {'provider': 'codex', 'model': 'cli-enqueue'}
                  for role in ('planner', 'cheap', 'standard', 'strong')},
    )
    # Do not start the scheduler thread or acquire the worker lock.
    # The CLI is a single-shot invocation; it enqueues to the durable
    # queue and exits.  The running service picks the job up.
    svc._ensure_scheduler = lambda: None
    # _submit normally calls _ensure_scheduler + queue.enqueue + wake.
    # We only need the durable enqueue.

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
    could be built without.  Before, this file constructed ``MaintenanceTasks``
    itself; a stop applied at the web layer would have left this door open, and
    "the CLI still creates tasks after the plugin was disabled" is precisely
    the failure the contract calls out.

    A Service is always constructed so cost accounting uses the same
    reconciliation logic the web surface uses (svc._usage).  When
    needs_dispatch is True (for create), svc.start_plan is also wired
    as the dispatch function.  For read-only commands, dispatch raises
    if somehow called.
    """
    from factory.control.issue_maintenance_webuddy import tasks_for
    svc = _make_service(store)
    dispatch = None if needs_dispatch else (
        lambda rid: (_ for _ in ()).throw(Conflict('此命令不需要调度器')))
    return tasks_for(svc, identity=LocalOperatorIdentity(operator_username),
                     dispatch=dispatch)


def _actor(username: str) -> dict:
    """Build the controlled actor dict from the verified local username."""
    return {'id': f'local:{username}', 'username': username}


# Commands that dispatch a new plan. This set gates ``dispatch`` only -- the
# resume and cancel hooks are wired unconditionally, so forgetting to add a
# command here can only produce a refusal, never a silent no-op.
_DISPATCH_COMMANDS = frozenset({'create'})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='maintenance-cli',
        description='Issue maintenance CLI (standalone, no webuddy).',
    )
    p.add_argument('--db', required=True, metavar='PATH',
                   help='Existing control.db (refuses to create)')
    p.add_argument('--operator', default=None, metavar='USER',
                   help='Operator username (must match OS user)')
    sub = p.add_subparsers(dest='command', required=True)

    cr = sub.add_parser('create', help='Create a maintenance task')
    cr.add_argument('--request-json', required=True, metavar='PATH',
                    help='JSON file with the maintenance request body')

    sub.add_parser('list',
                   help='List tasks for a project').add_argument(
        '--project', required=True)

    show = sub.add_parser('show', help='Show a single task')
    show.add_argument('task_id')

    ev = sub.add_parser('events', help='Task execution events')
    ev.add_argument('task_id')
    ev.add_argument('--after', type=int, default=0,
                    help='Only events after this sequence number')

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
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if not Path(args.db).is_file():
        print(json.dumps({'error': '数据库不存在；拒绝创建'},
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

    svc = None
    try:
        store = Store(args.db)
        needs_dispatch = args.command in _DISPATCH_COMMANDS
        tasks = _build_tasks(store, operator_username=operator,
                             needs_dispatch=needs_dispatch)
        actor = _actor(operator)
        result = _dispatch(args, tasks, actor)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (Conflict, KeyError, ValueError) as exc:
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


def _dispatch(args, tasks, actor: dict) -> dict:
    cmd = args.command

    if cmd == 'create':
        request_path = Path(args.request_json)
        if not request_path.is_file():
            raise ValueError(f'请求文件不存在：{request_path}')
        request = json.loads(request_path.read_text(encoding='utf-8'))
        view = tasks.create(request, actor=actor)
        return view

    if cmd == 'list':
        items = tasks.list(actor=actor, project_id=args.project)
        return {'tasks': items}

    if cmd == 'show':
        return tasks.get(args.task_id, actor=actor)

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


if __name__ == '__main__':
    raise SystemExit(main())
