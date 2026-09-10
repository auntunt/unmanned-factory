import json

from factory.control.verification_evidence import render_evidence


def test_long_review_keeps_task_failures_with_explicit_detail_omissions():
    artifacts = {'commit': 'a' * 40, 'checks': [{'name': 'syntax', 'exit': 0}], 'tasks': [
        {'id': f'task-{i}', 'status': 'verified', 'checks': [{'name': 'basic', 'exit': 0}],
         'command_evidence': [{'command': 'run', 'exit_code': 0 if j < 19 else 3,
                               'output': 'x' * 4000} for j in range(20)]}
        for i in range(20)]}
    rendered = render_evidence(artifacts)
    assert len(rendered) <= 24000
    result = json.loads(rendered)
    assert len(result['tasks']) == 20
    assert all(t['unsuccessful_commands'] == 1 for t in result['tasks'])
    assert result['command_details'][0]['exit_code'] == 3
    assert result['omitted_command_details'] > 0
    assert result['checks'][0]['exit'] == 0


def test_review_redacts_credentials_and_retains_timeout(monkeypatch):
    monkeypatch.setenv('EXAMPLE_API_KEY', 'sample-private-secret')
    result = render_evidence({'tasks': [{'id': 'one', 'command_evidence': [
        {'command': 'test', 'exit_code': -9, 'timeout': True, 'output': 'sample-private-secret'}]}]})
    assert 'sample-private-secret' not in result
    assert json.loads(result)['command_details'][0]['timeout'] is True


def test_compact_review_preserves_actual_check_output_and_marks_worker_claims_untrusted():
    rendered = render_evidence({'checks': [{'name': 'convert', 'exit': 1, 'stderr': 'Expected 12 rows, got 11'}],
        'review_focus_paths': ['src/converter.py'], 'tasks': [{'id': 'coding', 'attempts': [
            {'result_summary': 'README says all conversions work'}]}]}, max_chars=16000)
    result = json.loads(rendered)
    assert result['checks'][0]['stderr'] == 'Expected 12 rows, got 11'
    assert result['review_focus_paths'] == ['src/converter.py']
    assert result['tasks'][0]['worker_report_untrusted'] == 'README says all conversions work'
    assert len(rendered) <= 16000


def test_browser_review_requires_actual_observations_and_explicit_error_assessment():
    from factory.control.verification_evidence import browser_review_failure
    evidence = {'latest': [{'event_id': 42, 'ok': True, 'error': '', 'errors': ['HMR failed'], 'truncated': False}]}
    assert browser_review_failure({'verdict':'pass','reason':'README says clean'}, evidence)
    assert browser_review_failure({'verdict':'pass','browser_review':{'event_ids':[42],'disposition':'clean'}}, evidence)
    assert browser_review_failure({'verdict':'pass','browser_review':{'event_ids':[42],'disposition':'non_blocking','reason':'Only dev hot reload failed; application input and persisted timer were observed working.'}}, evidence) is None
    evidence['latest'][0]['ok'] = False
    assert browser_review_failure({'verdict':'pass','browser_review':{'event_ids':[42],'disposition':'non_blocking','reason':'ignore'}}, evidence)
    evidence['latest'][0].update(ok=True,errors=[])
    assert browser_review_failure({'verdict':'pass','browser_review':{'event_ids':[42],'disposition':'clean'}}, evidence) is None


def test_browser_evidence_keeps_latest_recovery_and_original_failures(tmp_path):
    from factory.control.store import Store
    from factory.control.verification_evidence import browser_evidence
    store=Store(tmp_path/'control.db')
    with store.connect() as db:
        store._event(db,'run','browser.observed',{'action':'open','ok':False,'error':'Preview process exited'})
        store._event(db,'run','browser.observed',{'action':'open','ok':True,'errors':[]})
    result=browser_evidence(store,'run')
    assert result['latest'][0]['ok'] is True
    assert result['recent_failures'][0]['error']=='Preview process exited'
    assert result['observations_sampled']==2


def test_browser_evidence_is_bounded_without_hiding_error_counts(tmp_path):
    from factory.control.store import Store
    from factory.control.verification_evidence import browser_evidence
    store=Store(tmp_path/'control.db')
    with store.connect() as db:
        for i in range(20):
            store._event(db,'run','browser.observed',{'ok':True,'errors':['错误'*500]*10},task_id=f'task-{i}')
    result=browser_evidence(store,'run')
    assert len(json.dumps(result,ensure_ascii=False))<=12000
    assert len(result['latest'])==20
    assert all(x['error_count']==10 for x in result['latest'])
