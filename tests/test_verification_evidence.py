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
