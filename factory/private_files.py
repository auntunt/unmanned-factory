"""Private creation defaults without changing process umask or existing access.

Existing installations are inspected by readiness diagnostics; silently chmoding
shared directories or already provisioned databases would change operator policy.
"""
from __future__ import annotations

import os
from pathlib import Path


def prepare_private_database(path: str | Path) -> None:
    if str(path) == ':memory:':
        return
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        if parent == parent.parent:
            break
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    else:
        os.close(fd)
