"""A complete parse must not silently choose between conflicting report fields."""
import json

import pytest

from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from tests.review_helpers import passing_review
from tests.test_control_app import app_env  # noqa: F401
from tests.test_verification_format_repair import _review


@pytest.mark.parametrize('conflict', ['duplicate_verdict', 'coverage_alias'])
def test_format_repair_cannot_resolve_conflicting_original_fields(app_env, conflict):
    _, _, service, repo, project, run, cfg = _review(app_env)
    responses = []

    def reviewer(request, emit, cancel=None):
        if not responses:
            clean = passing_review(request, 'observed evidence')
            responses.append(clean)
            if conflict == 'duplicate_verdict':
                # The later pass must not erase the earlier explicitly stated fail.
                original = '{"verdict":"fail",' + clean[1:-1] + ',}'
            else:
                data = json.loads(clean)
                data['acceptance_coverage'] = [
                    {**row, 'status': 'fail', 'evidence': 'original unresolved failure'}
                    for row in data['criteria']]
                original = json.dumps(data)[:-1] + ',}'
            return ProviderResult(original, cost_usd=.03)
        return ProviderResult(responses[0], cost_usd=.01)

    service.runner.run = reviewer
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError):
        service._independent_verify(run['id'], run, project, cfg, artifacts)
    assert artifacts.get('verification', {}).get('verdict') != 'pass'
