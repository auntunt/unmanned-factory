"""Bounded host command capacity shared across processes; models keep running."""
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import stat
import tempfile
import time


DEFAULT_COMMAND_SLOTS = 2
MAX_COMMAND_SLOTS = 8


def command_capacity():
    raw = os.getenv('WEBUDDY_COMMAND_SLOTS', str(DEFAULT_COMMAND_SLOTS))
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise ValueError('WEBUDDY_COMMAND_SLOTS must be an integer from 1 to 8') from None
    if not 1 <= value <= MAX_COMMAND_SLOTS:
        raise ValueError('WEBUDDY_COMMAND_SLOTS must be an integer from 1 to 8')
    return value


@contextmanager
def command_slot(timeout_s, cancel=None):
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s):
        raise ValueError('command capacity timeout must be finite')
    started = time.monotonic()
    capacity = command_capacity()
    descriptors = []
    try:
        for slot in range(capacity):
            # Slot zero retains the original lock pathname for rolling upgrades.
            suffix = '' if slot == 0 else f'-{slot}'
            path = Path(tempfile.gettempdir()) / f'webuddy-command-{os.getuid()}{suffix}.lock'
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError('command capacity lock must be a private regular file')
            os.fchmod(fd, 0o600)
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('command cancelled while waiting for capacity')
            elapsed = time.monotonic() - started
            if elapsed >= timeout_s:
                raise TimeoutError('command capacity wait exceeded deadline')
            acquired = False
            for fd in descriptors:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    continue
            if acquired:
                break
            delay = min(.05, timeout_s - elapsed)
            if cancel is not None and hasattr(cancel, 'wait'):
                cancel.wait(delay)
            else:
                time.sleep(delay)
        yield time.monotonic() - started
    finally:
        for fd in descriptors:
            os.close(fd)
