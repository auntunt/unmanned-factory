import json
from factory.control.providers import ProviderResult
from tests.test_control_app import app_env, login, project
from tests.test_active_verification import setup_review
from tests.review_helpers import passing_review
from tests.test_verification_browser_receipt import _stale_failure_then_clean, _prompt_observations

def test_resolved_snapshot_error_can_be_reconciled_without_recoding(app_env, monkeypatch):
    service, run, p, repo = setup_review(app_env)
    _stale_failure_then_clean(service, run['id'])
    calls=[]
    def reviewer(request, emit, cancel=None):
        calls.append(request)
        snapshot=_prompt_observations(request)
        emit('browser.observed', {'ok': True, 'action': 'screenshot', 'errors': []})
        verdict=json.loads(passing_review(request, 'Current browser cycle is clean; old favicon fixed'))
        verdict['browser_review']={'event_ids':[o['event_id'] for o in snapshot['latest']], 'disposition':'clean', 'reason':'A fresh verification observation now has no errors.'}
        return ProviderResult(json.dumps(verdict), cost_usd=.01)
    monkeypatch.setattr(service.runner,'run',reviewer)
    artifacts={'worktree':str(repo)}
    service._independent_verify(run['id'],run,p,service.runtime_settings.get(),artifacts)
    assert artifacts['verification']['verdict']=='pass'
    assert len(calls)<=2
