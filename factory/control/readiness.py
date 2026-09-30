"""Read-only local deployment preflight, not an enterprise certification.

No model request, network login, migration, chmod or service mutation is made.
The real sandbox canary uses temporary files and its own bounded subprocess.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import os
import shutil
import stat
import sys
from urllib.parse import unquote, urlsplit

from factory.control.store import now, scrub


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.paths.append(attrs['src'])
        if tag == 'link' and attrs.get('href') and attrs.get('rel') in ('stylesheet', 'modulepreload'):
            self.paths.append(attrs['href'])


def inspect_deployment(*, data_dir, workspace, static_dir, public_origin,
                       env_file=None, isolation_probe=None):
    checks = []

    def add(name, status, detail, action=None):
        item = {'id': name, 'status': status, 'detail': detail}
        if action:
            item['action'] = action
        checks.append(item)

    add('python', 'passed' if sys.version_info >= (3, 12) else 'blocked',
        'Python 3.12 or newer is required')
    add('git', 'passed' if shutil.which('git') else 'blocked', 'Git must be available to the service user')
    add('workspace', 'passed' if Path(workspace).is_dir() else 'blocked',
        'Configured workspace directory must already exist')
    if not public_origin:
        add('public_origin', 'unknown', 'Public origin was not supplied',
            'Set FACTORY_PUBLIC_ORIGIN or pass --public-origin with the production HTTPS origin')
    else:
        try:
            parsed = urlsplit(public_origin)
            valid = (parsed.scheme == 'https' and bool(parsed.hostname)
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment and parsed.path in ('', '/'))
            parsed.port  # Validate an explicitly supplied port without disclosing the value.
        except ValueError:
            valid = False
        add('public_origin', 'passed' if valid else 'blocked',
            'Production browser authentication requires a credential-free HTTPS origin',
            None if valid else 'Use the same HTTPS origin in the reverse proxy and service configuration')

    def private_path(name, path, directory=False, database=False):
        path = Path(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            add(name, 'blocked', 'Required data path is missing', 'Provision the intended service data directory first')
            return
        except OSError:
            add(name, 'unknown', 'Data path could not be inspected', 'Run diagnostics as the intended service user')
            return
        correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if not correct_type or path.is_symlink():
            add(name, 'blocked', 'Data path must be a real directory or regular file, not a symlink')
        elif os.name != 'posix':
            add(name, 'unknown', 'POSIX permissions cannot establish access on this platform',
                'Have an administrator verify the service-account ACLs')
        elif metadata.st_mode & 0o077:
            add(name, 'blocked', 'Data is accessible to group or other users',
                'An authorized operator must review ownership and restrict access; diagnostics do not chmod existing data')
        elif metadata.st_uid != os.geteuid():
            add(name, 'unknown', 'Data owner differs from the diagnostic user', 'Repeat as the configured service account')
        elif database:
            try:
                with path.open('rb') as stream:
                    valid_header = stream.read(16) == b'SQLite format 3\x00'
            except OSError:
                add(name, 'unknown', 'Database header could not be read')
            else:
                add(name, 'passed' if valid_header else 'blocked',
                    'Private SQLite file exists; business consistency still requires a restore drill'
                    if valid_header else 'Database is empty or not a SQLite file')
        else:
            add(name, 'passed', 'Private owner-only access is configured')

    data = Path(data_dir)
    private_path('data_directory', data, directory=True)
    for name in ('users.db', 'control.db'):
        private_path(name, data / name, database=True)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = data / (name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                private_path(name + suffix, sidecar)
    if env_file:
        private_path('environment_file', env_file)

    static = Path(static_dir).resolve()
    try:
        index = static / 'index.html'
        if index.is_symlink() or not index.is_file():
            raise FileNotFoundError
        parser = _Assets()
        parser.feed(index.read_text(encoding='utf-8'))
        local_ok = True
        external = False
        for reference in parser.paths:
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc:
                external = True
                continue
            target = (static / unquote(parsed.path).lstrip('/')).resolve()
            if not target.is_relative_to(static) or not target.is_file():
                local_ok = False
        add('frontend', 'blocked' if not local_ok else 'unknown' if external else 'passed',
            'HTML and referenced local scripts/styles checked; this is not browser acceptance',
            'Build frontend and verify all referenced assets' if not local_ok else
            'Verify external asset availability and policy separately' if external else None)
    except (OSError, UnicodeError, ValueError):
        add('frontend', 'blocked', 'Built frontend is missing or unreadable', 'Run the documented frontend build')

    try:
        if isolation_probe is None:
            from factory.control.pack_sandbox import probe
            isolation_probe = probe
        isolation = isolation_probe(refresh=True)
        add('execution_isolation', 'passed' if isolation.available else 'blocked',
            'Real execution-isolation canary passed' if isolation.available else
            str(scrub(isolation.reason, max_chars=1000)),
            None if isolation.available else 'Use a supported host where the isolation canary passes; do not disable the boundary')
    except Exception as exc:
        add('execution_isolation', 'unknown', 'Isolation probe could not complete: ' + type(exc).__name__)

    status = ('blocked' if any(item['status'] == 'blocked' for item in checks) else
              'unknown' if any(item['status'] == 'unknown' for item in checks) else 'passed')
    return {'schema_version': 1, 'checked_at': now(), 'scope': 'local_deployment_prerequisites',
            'status': status, 'checks': checks,
            'external_acceptance': [
                {'id': name, 'status': 'unknown', 'detail': detail} for name, detail in (
                    ('ci', 'The exact release commit must pass the repository CI gates'),
                    ('live_model', 'A real authorized provider task and independent verification must be demonstrated'),
                    ('browser', 'Login, role-scoped workflows, errors and recovery require browser acceptance'),
                    ('restore', 'A quiesced backup must be restored and business evidence checked in an isolated drill'),
                    ('https_proxy', 'The externally served TLS certificate and proxy need operator validation'),
                )],
            'note': 'Passed local prerequisites are not proof of enterprise readiness. No live settings or credentials were changed.'}
