"""Workflow history reaches acceptance as platform fact, traceable to event ids.

Acceptance had tool and browser observations but no platform record of whether this
run was interrupted, recovered, or changed by its owner. The only account of that
reaching the reviewer was whatever the worker wrote about itself, which is exactly the
claim a worker cannot be the source of.

Driven through the real `_independent_verify` to the final `ProviderRequest`, because
a helper that formats a dict proves nothing about what the reviewer is handed.
"""
import json
import threading

from factory.control.providers import ProviderResult
from factory.control.verification_evidence import workflow_evidence
from tests.review_helpers import passing_review
from tests.test_control_app import app_env, login, project


def _review_env(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '恢复后继续交付')
    run['agent_snapshot'] = {}
    service.cancels[run['id']] = threading.Event()
    cfg = service.runtime_settings.get()
    cfg['agent_verification_profile'] = {'provider': 'claude', 'model': 'review'}
    return client, store, service, repo, p, run, cfg


def _record_workflow(store, rid):
    """The durable rows a real interruption/recovery/intervention leaves behind."""
    store.append(rid, 'run.recovered', {'message': '执行已中断，保留工作区与检查点'})
    store.append(rid, 'run.resumed', {'phase': 'execute', 'execution_mode': 'continuous',
                                      'message': '已恢复持续编码检查点',
                                      'effective_revision': 2,
                                      'effective_digest': 'abc123',
                                      'effective_source': 'effective_contract'})
    store.append(rid, 'followup.applied', {'pending_id': 'f1', 'run_revision': 1,
                                           'effective_revision': 2,
                                           'effective_digest': 'abc123'})
    store.append(rid, 'execution.reused', {'stage': 'verification',
                                           'reason': '沿用同源检查结果'})
    # Noise that must not be sampled: ordinary progress is not workflow history.
    store.append(rid, 'task.activity', {'phase': 'checks'})


def _workflow_block(prompt):
    head = 'PLATFORM WORKFLOW RECORD'
    assert head in prompt
    body = prompt.split(head, 1)[1].split(':\n', 1)[1]
    return json.loads(body.split('\nOnly these records', 1)[0])


def test_the_final_review_request_carries_this_runs_recorded_workflow(app_env):
    client, store, service, repo, p, run, cfg = _review_env(app_env)
    rid = run['id']
    _record_workflow(store, rid)
    # Another run's history must not leak into this review.
    other, _ = store.create_run(p['id'], '别的任务')
    store.append(other['id'], 'run.resumed', {'phase': 'execute',
                                              'message': 'OTHER-RUN-LEAKED'})

    requests = []

    def respond(request, emit, cancel=None):
        requests.append(request)
        return ProviderResult(passing_review(request, '已按记录复核'), cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo), 'checks': [{'name': 'check', 'exit': 0}]}
    service._independent_verify(rid, run, p, cfg, artifacts)

    assert requests, 'the reviewer was never dispatched'
    prompt = requests[0].prompt
    block = _workflow_block(prompt)
    kinds = {item['type']: item for item in block['items']}
    assert set(kinds) == {'run.recovered', 'run.resumed', 'followup.applied',
                          'execution.reused'}
    assert 'task.activity' not in prompt.split('PLATFORM WORKFLOW RECORD', 1)[1]
    assert 'OTHER-RUN-LEAKED' not in prompt

    # Every item is traceable to the original record, and carries the agreement
    # association the recovery was bound to.
    ids = [item['event_id'] for item in block['items']]
    assert ids == sorted(ids)
    durable = {event['id']: event for event in store.events(rid)}
    for item in block['items']:
        assert durable[item['event_id']]['type'] == item['type']
    assert kinds['run.resumed']['effective_revision'] == 2
    assert kinds['run.resumed']['effective_digest'] == 'abc123'
    assert kinds['followup.applied']['pending_id'] == 'f1'
    assert kinds['run.recovered']['category'] == 'interruption'
    assert kinds['run.resumed']['category'] == 'recovery'
    assert kinds['followup.applied']['category'] == 'intervention'
    assert kinds['execution.reused']['category'] == 'recovery'
    # The reviewer is told these are the only admissible source for the claim.
    assert 'worker report' in prompt and 'skill body' in prompt
    assert artifacts['verification_workflow_event_ids'] == ids


def test_the_summary_is_bounded_and_says_what_it_dropped(app_env):
    """A long history is excerpted, never handed over whole, and admits the excerpt."""
    client, store, service, repo, p, run, cfg = _review_env(app_env)
    rid = run['id']
    for index in range(60):
        store.append(rid, 'run.resumed', {'phase': 'execute',
                                          'message': f'恢复第 {index} 次' + 'x' * 200})
    record = workflow_evidence(store, rid, max_items=40, max_chars=6000)
    assert len(json.dumps(record, ensure_ascii=False)) <= 6000
    assert record['recorded_total'] == 60
    assert record['omitted_older'] == 60 - len(record['items'])
    assert record['omitted_older'] > 0
    # Newest kept, oldest dropped: the most recent recovery is what a reviewer needs.
    assert record['items'][-1]['message'].startswith('恢复第 59 次')
