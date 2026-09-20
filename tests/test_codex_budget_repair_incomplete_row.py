"""A malformed evidence row must not vanish during format-only repair."""
import json
import pytest
from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from tests.review_helpers import passing_review
from tests.test_control_app import app_env
from tests.test_verification_format_repair import _review


def test_format_repair_does_not_drop_incomplete_conflicting_row(app_env):
    _, _, service, repo, p, run, cfg = _review(app_env)
    calls = []
    repaired = None

    def respond(request, emit, cancel=None):
        nonlocal repaired
        calls.append(request)
        if len(calls) == 1:
            repaired = passing_review(request, 'observed evidence')
            data = json.loads(repaired)
            data['criteria'].append({'id': data['criteria'][0]['id'], 'status': 'fail'})
            damaged = json.dumps(data)[:-1] + ',}'
            return ProviderResult(damaged, cost_usd=0.03)
        return ProviderResult(repaired, cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts.get('verification', {}).get('verdict') != 'pass'
