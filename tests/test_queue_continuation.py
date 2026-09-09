import threading
import time
from pathlib import Path

import pytest

from tests.test_autonomous_service import control, _autonomous, _new_dag_run, _plan, _wait


def test_answer_arriving_before_prior_worker_releases_slot_is_not_lost(control, monkeypatch):
    client, store, service, runner, project, headers = control
    runner.plans = [_plan(questions=['Choose a greeting.']), _plan()]
    _autonomous(client, project, headers)
    planned = threading.Event()
    release = threading.Event()
    original = service._plan

    def held_plan(rid):
        original(rid)
        if not planned.is_set():
            planned.set()
            assert release.wait(10)

    monkeypatch.setattr(service, '_plan', held_plan)
    try:
        rid = _new_dag_run(control)
        assert planned.wait(10)
        assert store.get(rid)['status'] == 'needs_clarification'
        response = client.post(f'/api/v2/runs/{rid}/clarify', json={'answer': 'Use hello autonomous.'}, headers=headers)
        assert response.status_code == 200, response.text
        assert any(job['run_id'] == rid and job['phase'] == 'plan' for job in service.queue.pending())
        release.set()
        final = _wait(store, rid, {'ready_for_review', 'needs_human'})
        assert final['status'] == 'ready_for_review', store.events(rid)
        assert runner.planner_calls == 2 and runner.worker_calls == 1
    finally:
        release.set()


def test_replanning_after_failed_execution_keeps_workspace_and_all_costs(control, monkeypatch):
    client, store, service, runner, project, headers = control
    _autonomous(client, project, headers, attempts=1)
    original = runner.run

    def fail_first_worker(request, emit, cancel=None):
        first = not request.read_only and runner.worker_calls == 0
        result = original(request, emit, cancel)
        if first:
            Path(request.workspace, 'greeting.txt').write_text('failed attempt')
        return result

    monkeypatch.setattr(runner, 'run', fail_first_worker)
    rid = _new_dag_run(control)
    failed = _wait(store, rid, {'needs_human'})
    prior_workspace = Path(failed['artifacts']['worktree'])
    assert prior_workspace.exists()
    response = client.post(f'/api/v2/runs/{rid}/clarify', json={'answer': 'Repair the check failure with the same target.'}, headers=headers)
    assert response.status_code == 200, response.text
    final = _wait(store, rid, {'ready_for_review', 'needs_human'})
    assert final['status'] == 'ready_for_review', store.events(rid)
    assert final['artifacts']['worktree'] != str(prior_workspace)
    assert prior_workspace.exists()
    assert final['artifacts']['total_known_cost_usd'] == pytest.approx(1.0)
    assert final['artifacts']['planner_cost_usd'] == pytest.approx(0.4)
    assert runner.planner_calls == runner.worker_calls == 2
