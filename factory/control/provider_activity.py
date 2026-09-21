"""Positive evidence about whether a provider worker is still writing a workspace.

A restarted coordinator cannot ask the previous one anything: the process is gone.
It also must not infer the answer. A web socket dropping says nothing about the
worker. A pid says nothing either -- absent, it may have been reaped after doing its
writes; present, the number may have been reused. And durable status is exactly what
a crash leaves stale, so reading `running` from the database proves only that nobody
got to write the next row.

The worker itself holds an exclusive OS lock on a file beside its worktree for as
long as its process lives. The kernel releases that lock when the process dies, for
any reason, including SIGKILL, so acquiring it is a fact about the process rather
than a claim anyone recorded. A coordinator that cannot acquire it knows a worker is
alive; one that can knows the previous worker is gone. Neither answer is a guess.

Advisory locks are per open file description, so a caller checking its own live
worker's lock would see it held -- which is the intended answer here, since that
worker is indeed still writing.
"""
from __future__ import annotations

import fcntl
from pathlib import Path

# Kept next to the worktree, not inside it: an untracked file in the tree would
# show up in the worker's own change guard and in the diff a reviewer reads.
LOCK_NAME = 'provider-activity.lock'


def lock_path_for(worktree) -> str:
    return str(Path(worktree).parent / LOCK_NAME)


def hold(path):
    """Take the activity lock for this process's lifetime, or report the holder.

    Returns the open handle, which the caller must keep referenced until it exits;
    the lock is released by process death, not by any bookkeeping we could skip.
    Raises BlockingIOError when another worker still holds it, which means a second
    writer was about to start against a workspace that already has one.
    """
    handle = Path(path).open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise
    return handle


def is_active(path) -> bool:
    """True when some live process still holds this workspace's activity lock.

    A missing file is not activity: the lock is created by the worker, so its
    absence means no worker ever started here. Any other error reading it is
    reported as active, because an unreadable lock is not evidence of an exit.
    """
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
