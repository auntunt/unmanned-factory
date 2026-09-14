"""Preserve structured failure causes through coordinator exception wrappers."""
import subprocess

from factory.harness.proc import Timeout as ProcessTimeout


FAILURE_CATEGORIES = {
    'budget': 'budget', 'browser_unavailable': 'environment',
    'BrowserUnavailable': 'environment', 'timeout': 'timeout',
    'TimeoutError': 'timeout', 'TimeoutExpired': 'timeout',
    'interrupted': 'interrupted', 'InterruptedError': 'interrupted',
    'PermissionError': 'access', 'access': 'access',
}


def failure_type(exc):
    seen = set()
    evidence_type = None
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, 'error_type', None):
            return exc.error_type
        if getattr(exc, 'error_kind', None) == 'timeout':
            return 'timeout'
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired, ProcessTimeout)):
            return 'timeout'
        if isinstance(exc, InterruptedError):
            return 'interrupted'
        if isinstance(exc, PermissionError):
            return 'access'
        artifacts = getattr(exc, 'artifacts', None) or {}
        evidence_type = evidence_type or (artifacts.get('verification') or {}).get('error_type')
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return evidence_type
