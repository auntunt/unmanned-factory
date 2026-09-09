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
