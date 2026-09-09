"""Read-only discovery of managed Git workspaces; browser never supplies a path."""
from __future__ import annotations
import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def _git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ''


def discover(root: Path, projects: list[dict]):
    if not root.is_dir():
        return []
    found = []
    # The managed root contains independent projects, not an arbitrary filesystem tree.
    for child in sorted(root.iterdir(), key=lambda p: p.name)[:200]:
        if child.name.startswith('.') or child.is_symlink() or not child.is_dir():
            continue
        path = child.resolve()
        if not path.is_relative_to(root) or not (path / '.git').exists():
            continue
        top = _git(path, 'rev-parse', '--show-toplevel')
        if not top or Path(top).resolve() != path:
            continue
        branch = _git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD')
        if not branch or not _git(path, 'rev-parse', '--verify', 'refs/heads/' + branch):
            continue
        remote = _git(path, 'remote', 'get-url', 'origin')
        repository = ''
        if remote.startswith('git@github.com:'):
            repository = remote[len('git@github.com:'):]
        else:
            parsed = urlparse(remote)
            if parsed.hostname == 'github.com' and parsed.scheme in ('https', 'ssh'):
                repository = parsed.path.lstrip('/')
        repository = repository.removesuffix('.git')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
            slug = re.sub('[^A-Za-z0-9_.-]', '-', child.name).strip('-._')[:60] or 'project'
            repository = 'local/' + slug + '-' + hashlib.sha256(str(path).encode()).hexdigest()[:10]
        existing = next((p for p in projects if Path(p['workspace']).resolve() == path or p['repository'] == repository), None)
        # Branch/remote changes invalidate a previously selected candidate.
        identity = '\0'.join((str(path), branch, repository))
        found.append({'id': hashlib.sha256(identity.encode()).hexdigest(), 'name': child.name,
                      'repository': repository, 'base_branch': branch, 'workspace': str(path),
                      'registered': existing is not None, 'project_id': existing['id'] if existing else None})
    return found
