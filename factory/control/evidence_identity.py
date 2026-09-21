"""What a recorded check result is actually evidence *about*.

A passing check is only evidence for the exact thing it ran against. The
checkpoint already pinned the source: a saved finalization is refused when the
changed paths or their content hash moved. Three other inputs decide the same
outcome and were not pinned at all.

- **The command.** A saved `pass` for `pytest -q` is not evidence for
  `pytest -q --strict`, and the whole-checkpoint guard is all-or-nothing: one
  edited check discards every other check's valid result, while a check whose
  own argv is unchanged has no reason to run again.
- **The tool and environment that ran it.** The same argv against a different
  interpreter, a different `PATH`, or a different virtualenv is a different
  check. Reusing across that is how a green result survives the change that
  would have broken it.
- **The agreement it was judged against.** A supplement revises what the run
  owes. Carrying an old `pass` forward under the new revision re-labels
  evidence that never covered the new requirement; coverage has to be
  re-established, not renamed.

So an identity is recorded alongside each result, and reuse is decided per
check against a freshly computed one. Anything whose identity cannot be
established -- a legacy checkpoint, an unresolvable executable -- is treated as
unverified and re-run, because "we cannot tell" must never read as "it passed".
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from factory.control.store import now

# Environment variables that decide which tool runs and what it imports. A
# change to any of them makes the same argv a different check.
ENV_KEYS = ('PATH', 'PYTHONPATH', 'VIRTUAL_ENV', 'PYTHONHOME', 'NODE_PATH',
            'CARGO_HOME', 'GOPATH', 'JAVA_HOME')

#: How long a remote health observation stays current. Beyond this it is a
#: record of the past, not evidence about the deployment as it is now.
HEALTH_TTL_S = 900


#: Above this, a file is identified by size alone rather than by content. Check
#: scripts and interpreters are far smaller; the cap only stops an identity
#: computation from reading an arbitrarily large argument off disk.
MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024


def _file_fingerprint(path: Path) -> list | None:
    """Identify a file by what is in it, not by when it was last written.

    Size plus mtime was not enough: a build system, a `tar -p` extraction, a
    `cp -p`, or a checkout that restores timestamps can put a different
    executable at the same path with the same recorded stat, and the identity
    would then read as unchanged across a tool swap -- exactly the case this
    module exists to catch. The content digest is the fingerprint; size stays in
    the record so an unreadable-but-present file is still distinguishable.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size > MAX_FINGERPRINT_BYTES:
        # Not read, therefore not identified. Reporting a stat-only record here
        # would make two different files indistinguishable again.
        return None
    hasher = hashlib.sha256()
    try:
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                hasher.update(block)
    except OSError:
        return None
    return [str(path), stat.st_size, hasher.hexdigest()]


def _tool_fingerprint(root, argv, env):
    """Identify the executable this argv would actually reach, and how.

    A relative path is resolved against the check's working directory, not this
    process's -- a check runs in the workspace, so `./run.sh` means the one there.
    """
    name = argv[0] if argv else None
    if name and not os.path.isabs(name) and os.sep in name:
        candidate = Path(root) / name
        resolved = str(candidate) if candidate.is_file() else None
    else:
        resolved = shutil.which(name, path=env.get('PATH')) if name else None
    if resolved is None:
        # Unresolvable now: refuse to claim identity rather than compare against
        # a name that may resolve to something else next time.
        return None
    fingerprint = _file_fingerprint(Path(resolved))
    if fingerprint is None:
        # The tool is there but its content could not be read. "We cannot tell
        # which tool this is" is not an identity; saying so keeps the result from
        # being reused across a swap we would not have seen.
        return None
    return {'argv0': argv[0], 'resolved': fingerprint,
            'env': {key: env.get(key) for key in ENV_KEYS}}


def _input_fingerprint(root: Path, argv):
    """Bind the check to the input files it names, so editing one invalidates it.

    Only arguments that resolve to a real file inside the workspace count. A
    fixture or test file passed on the command line is part of what the result
    means; an unrelated flag is not.
    """
    out = {}
    base = Path(root).resolve()
    for item in argv[1:]:
        if not item or item.startswith('-'):
            continue
        candidate = (base / item) if not os.path.isabs(item) else Path(item)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file() or not resolved.is_relative_to(base):
            continue
        out[item] = _file_fingerprint(resolved)
    return out


def check_identity(root, name, argv, *, code_signature, paths,
                   revision=None, digest=None, env=None):
    """The identity a result for this check would be evidence for, or None.

    None means the identity could not be established -- the caller must treat
    any saved result as unverified.
    """
    argv = list(argv or ())
    if not argv:
        return None
    if env is None:
        # The environment the check will really get, not this process's. A check
        # runs under `check_env`, so that is what decides which tool it reaches.
        from factory.harness.checkenv import check_env
        env = check_env()
    environment = dict(env)
    tool = _tool_fingerprint(root, argv, environment)
    if tool is None:
        return None
    return {'version': 1, 'name': name, 'argv': argv, 'tool': tool,
            'inputs': _input_fingerprint(root, argv),
            'code': {'signature': code_signature, 'paths': sorted(paths or ())},
            'requirement': {'revision': revision, 'digest': digest}}


def fingerprint(identity) -> str | None:
    """A stable key for one identity, for comparison and for the record."""
    if not isinstance(identity, dict):
        return None
    return hashlib.sha256(json.dumps(identity, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def reusable(record, identity):
    """Whether a saved check result is evidence for `identity`. (ok, reason).

    Conservative in both unknown directions: a result with no recorded identity
    (written before this existed, or by a path that could not establish one) is
    not reusable, and neither is a result whose identity cannot be computed now.
    """
    if not isinstance(record, dict):
        return False, 'no saved result'
    if record.get('cancelled') or record.get('timeout') or record.get('exit') != 0:
        return False, 'saved result did not pass'
    saved = record.get('identity_fingerprint')
    if not saved:
        return False, 'saved result has no recorded identity'
    current = fingerprint(identity)
    if current is None:
        return False, 'identity cannot be established now'
    if saved != current:
        return False, 'identity changed since that result'
    return True, 'same identity'


def partition(records, identities):
    """Split configured checks into those already covered and those still owed.

    A change invalidates only the checks whose own identity moved. One edited
    check no longer discards every other check's valid result.
    """
    by_name = {}
    for record in records or ():
        if isinstance(record, dict) and record.get('name'):
            by_name[record['name']] = record
    reuse, rerun = [], []
    for name, identity in identities.items():
        ok, reason = reusable(by_name.get(name), identity)
        (reuse if ok else rerun).append({'name': name, 'reason': reason})
    return reuse, rerun


def health_is_current(observed_at, *, ttl_s=HEALTH_TTL_S, at=None):
    """A deployment health observation expires; absence of a time does not pass."""
    if not observed_at:
        return False
    from datetime import datetime
    try:
        seen = datetime.fromisoformat(str(observed_at).replace('Z', '+00:00'))
        current = datetime.fromisoformat(str(at or now()).replace('Z', '+00:00'))
    except ValueError:
        return False
    if seen.tzinfo is None or current.tzinfo is None:
        return False
    return 0 <= (current - seen).total_seconds() <= ttl_s
