#!/usr/bin/env python3
"""Create/verify a local two-database snapshot. This is NOT a full-service backup.

The operator must stop all writers for cross-database consistency. This helper
never stops services, alters the source, copies external credential files, or
restores files. Database snapshots contain sensitive account/session data.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

DATABASES = ('control.db', 'users.db')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def quick_check(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('PRAGMA quick_check').fetchall()
    if rows != [('ok',)]:
        raise ValueError('SQLite integrity check failed; inspect locally under authorized access')


def snapshot(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Source must be an existing real directory')
    for name in DATABASES:
        p = source / name
        if not p.is_file() or p.is_symlink():
            raise ValueError('Both existing non-symlink databases are required')
    source, destination = source.resolve(), destination.absolute()
    if destination.resolve().is_relative_to(source):
        raise ValueError('Destination must be outside the live data directory')
    # Never overwrite or mix snapshots. Restrict new files before creation.
    old_mask = os.umask(0o077)
    try:
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'SQLite only; not cross-database atomic; requires quiesced writers',
                    'databases': []}
        for name in DATABASES:
            target = destination / name
            with closing(sqlite3.connect((source / name).as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
                    # Backup inherits WAL mode. Normalize to a closed single-file
                    # snapshot before hashing, or later WAL writes evade the hash.
                    if dst.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                        raise ValueError('Cannot finalize a single-file snapshot')
            quick_check(target)
            manifest['databases'].append({'file': name, 'bytes': target.stat().st_size,
                                          'sha256': digest(target)})
        # Written only on full success. Failed attempts remain for inspection.
        (destination / 'snapshot.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    finally:
        os.umask(old_mask)
    return manifest


def verify(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Snapshot directory must not be a symlink')
    mpath = directory / 'snapshot.json'
    if mpath.is_symlink():
        raise ValueError('Snapshot manifest must not be a symlink')
    manifest = json.loads(mpath.read_text())
    if not isinstance(manifest, dict):
        raise ValueError('Unexpected snapshot manifest')
    entries = manifest.get('databases', [])
    if (not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries)
            or manifest.get('format') != 1 or [e.get('file') for e in entries] != list(DATABASES)):
        raise ValueError('Unexpected snapshot manifest')
    for entry in entries:
        p = directory / entry['file']
        if p.is_symlink() or not p.is_file():
            raise ValueError('Snapshot database missing or unsafe')
        if any(Path(str(p) + suffix).exists() or Path(str(p) + suffix).is_symlink()
               for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('Snapshot has SQLite sidecars; it is not a closed immutable snapshot')
        with closing(sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                raise ValueError('Snapshot is not in single-file journal mode')
        if p.stat().st_size != entry['bytes'] or digest(p) != entry['sha256']:
            raise ValueError('Snapshot size or SHA-256 mismatch')
        quick_check(p)
    return manifest


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['verify']:
        parser = argparse.ArgumentParser(description='Verify a SQLite-only snapshot manifest and integrity')
        parser.add_argument('--snapshot', required=True)
        args = parser.parse_args(argv[1:])
        result = verify(args.snapshot)
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-dir', required=True)
        parser.add_argument('--destination', required=True)
        args = parser.parse_args(argv)
        result = snapshot(args.source_dir, args.destination)
    print(json.dumps({'ok': True, 'databases': len(result['databases']),
                      'scope': result['scope']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f'Snapshot failed ({type(exc).__name__}); source unchanged; incomplete destination must not be used', file=sys.stderr)
        sys.exit(1)
