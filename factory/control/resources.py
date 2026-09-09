"""One host command slot shared by checks and SDK subprocesses; models keep running."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import time


@contextmanager
def command_slot(timeout_s, cancel=None):
    started = time.monotonic()
    path = Path(tempfile.gettempdir()) / f'webuddy-command-{os.getuid()}.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('command cancelled while waiting for capacity')
            elapsed = time.monotonic() - started
            if elapsed >= timeout_s:
                raise TimeoutError('command capacity wait exceeded deadline')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.05, timeout_s - elapsed))
        yield time.monotonic() - started
    finally:
        os.close(fd)
