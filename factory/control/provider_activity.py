"""Positive evidence about whether a provider worker is still writing a workspace.

A restarted coordinator cannot ask the previous one anything: the process is gone.
It also must not infer the answer. A web socket dropping says nothing about the
worker. A pid says nothing either -- absent, it may have been reaped after doing its
writes; present, the number may have been reused. And durable status is exactly what
a crash leaves stale, so reading `running` from the database proves only that nobody
got to write the next row.

The worker holds an exclusive OS lock on a file beside its worktree for as long as
its process lives. The kernel releases that lock when the process dies, for any
reason, including SIGKILL, so *not* being able to acquire it is a fact about a live
process rather than a claim anyone recorded.

Acquiring it, however, proves less than it looks. The worker is a wrapper around an
SDK that starts its own children, and those children keep running -- and keep
writing the tree -- when the wrapper is SIGKILLed. The lock is per open file
description, so it drops with the wrapper while its descendants write on. That is
why a free lock alone is not an exit: the writer is a process *tree*, not a process.

So the holder also registers that tree durably: it makes itself a process group
leader and records the group id beside the lock. The group outlives the wrapper and
is inherited by every descendant that does not deliberately leave it, so
``killpg(pgid, 0)`` asks the kernel about the whole writer, wrapper gone or not.
Three answers, and only one of them is an exit: group gone means gone, group alive
means still writing, and anything we cannot read or interpret keeps the workspace
blocked. The launcher registers the group before the worker can and clears the
record only after it has confirmed the group is empty, so the windows around start
and finish are covered rather than assumed.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

# Kept next to the worktree, not inside it: an untracked file in the tree would
# show up in the worker's own change guard and in the diff a reviewer reads.
LOCK_NAME = 'provider-activity.lock'


def lock_path_for(worktree) -> str:
    return str(Path(worktree).parent / LOCK_NAME)


def record_path_for(path) -> Path:
    """The durable tree record that belongs to this lock."""
    return Path(path).with_suffix('.record.json')


def _own_group() -> int | None:
    """Make this process a group leader so its descendants are identifiable.

    A process that shares its group with its launcher cannot be asked about
    separately: the group stays alive because the launcher does. `setsid` fails
    when we already lead, which is the state the launcher's `start_new_session`
    leaves us in, so the check comes first and the failure is not fatal.
    """
    if os.name != 'posix':
        return None
    try:
        if os.getpgid(0) != os.getpid():
            os.setsid()
        return os.getpgid(0)
    except OSError:
        try:
            return os.getpgid(0)
        except OSError:
            return None


def group_alive(pgid) -> bool | None:
    """Does any process remain in this group? None when the answer is not ours.

    Signal 0 is delivered to nobody; the error is the whole answer. A group we
    are ourselves part of cannot be asked -- we would be answering about
    ourselves -- and a group owned by another user is alive but unsignalable.
    """
    if os.name != 'posix' or not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return None
    try:
        if os.getpgid(0) == pgid:
            return None
    except OSError:
        pass
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def register(path, pgid, *, role: str = 'launcher') -> None:
    """Record the process group that is about to write, before it can do so.

    Called by the launcher with the group it just created: between the fork and
    the worker taking its own lock there is a window in which the tree is real
    and no lock exists yet. Written whole via replace so a reader never sees half
    a record.
    """
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return
    record = record_path_for(path)
    payload = json.dumps({'pgid': pgid, 'role': role, 'at': time.time()})
    temporary = record.with_name(record.name + f'.{os.getpid()}.tmp')
    try:
        temporary.write_text(payload, encoding='utf-8')
        os.replace(temporary, record)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def registered(path) -> int | None | bool:
    """The recorded group id, None when there is no record, False when unreadable.

    An unreadable record is not an absent one: it is a tree we know was
    registered and can no longer identify, which has to keep the site blocked.
    """
    record = record_path_for(path)
    try:
        raw = record.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        pgid = json.loads(raw).get('pgid')
    except (ValueError, AttributeError):
        return False
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    return pgid


def retire(path, pgid, *, timeout_s: float = 2.0) -> bool:
    """Drop the record only once the kernel says that tree is empty.

    A controlled shutdown has to leave the workspace recoverable, so the record
    cannot be permanent; an abnormal one must not, so it cannot be cleared on
    trust either. The group draining is the only thing that clears it.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if group_alive(pgid) is False:
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    current = registered(path)
    if current is False:
        return False
    if isinstance(current, int) and current != pgid and group_alive(current) is not False:
        # The worker re-grouped itself, or a new writer registered: either way the
        # record now names a tree the kernel has not told us is empty.
        return False
    try:
        record_path_for(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def hold(path):
    """Take the activity lock for this process's lifetime, or report the holder.

    Returns the open handle, which the caller must keep referenced until it exits;
    the lock is released by process death, not by any bookkeeping we could skip.
    Raises BlockingIOError when another worker still holds it, which means a second
    writer was about to start against a workspace that already has one.

    The holder also becomes a process group leader and registers that group, so
    the descendants it is about to start remain identifiable after it dies.
    """
    handle = Path(path).open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise
    pgid = _own_group()
    if pgid is not None:
        register(path, pgid, role='worker')
    return handle


def is_active(path) -> bool:
    """True while any process of a previous writer's tree may still be writing.

    Three sources, and the pessimistic one wins. The recorded process group
    answers for descendants that outlived their wrapper; the lock answers for a
    holder that never got to register one; and anything unreadable in either is
    reported as active, because absence of evidence is not an exit.
    """
    tree = registered(path)
    if tree is False:
        return True
    if isinstance(tree, int):
        alive = group_alive(tree)
        if alive or alive is None:
            return True
    file = Path(path)
    if not file.exists():
        return False
    try:
        handle = file.open('a+')
    except OSError:
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return True
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()
