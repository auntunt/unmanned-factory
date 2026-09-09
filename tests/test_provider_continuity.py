"""Provider continuity metadata crosses real subprocess errors without hidden retries."""
import json
import sys
import types

import pytest

from factory.control.providers import (
    ProviderCancelled, ProviderError, ProviderRequest, ProviderTimeout, SDKRunner,
    _run_claude, failure_metadata,
)
from tests.test_control_providers import _worker


@pytest.mark.parametrize('message,transient,kind', [
    ('API Error: 504 Gateway Time-out', True, 'upstream_unavailable'),
    ('HTTP 502 bad gateway', True, 'upstream_unavailable'),
    ('status_code=503', True, 'upstream_unavailable'),
    ('Error code: 429', True, 'rate_limit'),
    ('HTTP/1.1 529 overloaded', True, 'upstream_unavailable'),
    ('connection reset by peer', True, 'network'),
    ('HTTP 401 unauthorized', False, 'provider_error'),
    ('HTTP 403 forbidden', False, 'provider_error'),
    ('assert expected 503 but got 504', False, 'provider_error'),
    ('process exited with code 429', False, 'provider_error'),
    ('TypeError in application code', False, 'provider_error'),
    ('Claude SDK completed without an assistant result', False, 'provider_error'),
])
def test_transport_classification_does_not_retry_code_errors(message, transient, kind):
    error = ProviderError(message)
    assert error.transient is transient and error.error_kind == kind


def test_structured_errors_and_timeouts_are_distinct_from_cancel():
    class APIStatusError(Exception):
        status_code = 503
    assert failure_metadata(APIStatusError('upstream temporarily unavailable')) == {
        'transient': True, 'error_kind': 'upstream_unavailable', 'status_code': 503}
    assert failure_metadata('disconnected', exception_kind='APIConnectionError')['transient']
    assert ProviderTimeout('deadline').transient
    assert not ProviderCancelled('stop', transient=True).transient


def test_transient_worker_failure_retains_session_and_same_workspace_resume(tmp_path):
    python, script = _worker(tmp_path, '''
        import json, pathlib, sys
        req = json.loads(sys.stdin.readline())
        workspace = pathlib.Path(req['workspace'])
        marker = workspace / 'already-written.txt'
        if not req.get('session_id'):
            marker.write_text('preserve first attempt')
            print(json.dumps({'type':'provider.session','payload':{'session_id':'continuous-session'}}), flush=True)
            print(json.dumps({'type':'error','payload':{'message':'API Error: 504 Gateway Timeout'}}), flush=True)
            raise SystemExit(3)
        assert req['session_id'] == 'continuous-session'
        assert marker.read_text() == 'preserve first attempt'
        print(json.dumps({'type':'complete','payload':{'text':'resumed successfully'}}), flush=True)
    ''')
    runner = SDKRunner(worker_command=[python, str(script)])
    events = []
    with pytest.raises(ProviderError) as failed:
        runner.run(ProviderRequest('claude', 'same-model', 'do task', str(tmp_path)), lambda *x: events.append(x))
    assert failed.value.session_id == 'continuous-session' and failed.value.transient
    assert events == [('provider.session', {'session_id': 'continuous-session'})]
    result = runner.run(ProviderRequest('claude', 'same-model', 'continue from files', str(tmp_path),
        session_id=failed.value.session_id), lambda *_: None)
    assert result.session_id == 'continuous-session' and result.text == 'resumed successfully'


def test_timeout_retains_latest_session_and_identity_repeats_are_not_progress(tmp_path):
    python, script = _worker(tmp_path, '''
        import json, time
        for _ in range(3):
            print(json.dumps({'type':'provider.session','payload':{'session_id':'current-session'}}), flush=True)
        time.sleep(10)
    ''')
    events = []
    with pytest.raises(ProviderTimeout) as error:
        SDKRunner(worker_command=[python, str(script)]).run(
            ProviderRequest('claude', 'm', 'p', str(tmp_path), session_id='old-session', timeout_s=.2),
            lambda *x: events.append(x))
    assert error.value.session_id == 'current-session' and error.value.transient
    assert len(events) == 1


def test_protocol_error_retains_session_without_becoming_transient(tmp_path):
    python, script = _worker(tmp_path, '''
        import json
        print(json.dumps({'type':'provider.session','payload':{'session_id':'s'}}), flush=True)
        print('[]', flush=True)
    ''')
    with pytest.raises(ProviderError) as error:
        SDKRunner(worker_command=[python, str(script)]).run(ProviderRequest('claude', 'm', 'p', str(tmp_path)), lambda *_: None)
    assert error.value.session_id == 's' and not error.value.transient


def test_structured_worker_error_retains_network_classification(tmp_path):
    python, script = _worker(tmp_path, '''
        import json
        print(json.dumps({'type':'error','payload':{'message':'request failed','kind':'APIStatusError',
            'status_code':503,'session_id':'s','transient':True,'error_kind':'upstream_unavailable'}}), flush=True)
        raise SystemExit(4)
    ''')
    with pytest.raises(ProviderError) as error:
        SDKRunner(worker_command=[python, str(script)]).run(ProviderRequest('claude', 'm', 'p', str(tmp_path)), lambda *_: None)
    assert error.value.session_id == 's' and error.value.status_code == 503 and error.value.transient


def test_isolated_command_evidence_survives_worker_transport(tmp_path):
    python, script = _worker(tmp_path, '''
        import json
        print(json.dumps({'type':'command.completed','payload':{'command':'python test.py','exit_code':0,'output':'passed'}}), flush=True)
        print(json.dumps({'type':'complete','payload':{'text':'done'}}), flush=True)
    ''')
    events = []
    SDKRunner(worker_command=[python, str(script)]).run(ProviderRequest('claude', 'm', 'p', str(tmp_path)), lambda *x: events.append(x))
    assert events == [('command.completed', {'command':'python test.py', 'exit_code':0, 'output':'passed'})]


def test_claude_native_failure_retains_observed_session_and_resume_options(monkeypatch, tmp_path):
    from factory.control import claude_terminal
    from factory.control.sdk_worker import _failure_payload
    monkeypatch.setattr(claude_terminal, 'available', lambda: False)
    module = types.ModuleType('claude_agent_sdk')
    class Options:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class SystemMessage:
        data = {'session_id':'new-session'}
    class APIStatusError(Exception):
        status_code = 504
    captured = []
    async def query(*, prompt, options):
        captured.append(options)
        yield SystemMessage()
        yield SystemMessage()
        raise APIStatusError('gateway unavailable')
    module.ClaudeAgentOptions = module.HookMatcher = module.PermissionResultAllow = module.PermissionResultDeny = Options
    module.query = query
    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', module)
    events = []
    with pytest.raises(APIStatusError) as error:
        _run_claude(ProviderRequest('claude', 'm', 'p', str(tmp_path), session_id='previous-session'), lambda *x: events.append(x))
    assert error.value.session_id == 'new-session'
    assert captured[0].resume == 'previous-session'
    assert captured[0].cwd == str(tmp_path.resolve())
    assert events.count(('provider.session', {'session_id':'new-session'})) == 1
    payload = _failure_payload(error.value)
    assert payload['status_code'] == 504 and payload['transient'] and payload['session_id'] == 'new-session'


def test_cancellation_retains_session_but_never_retries(tmp_path):
    import threading
    python, script = _worker(tmp_path, '''
        import json, time
        print(json.dumps({'type':'provider.session','payload':{'session_id':'cancel-session'}}), flush=True)
        time.sleep(10)
    ''')
    cancel = threading.Event()
    def emit(kind, payload):
        if kind == 'provider.session': cancel.set()
    with pytest.raises(ProviderCancelled) as error:
        SDKRunner(worker_command=[python, str(script)]).run(ProviderRequest('claude', 'm', 'p', str(tmp_path)), emit, cancel)
    assert error.value.session_id == 'cancel-session' and not error.value.transient


def test_latest_observed_session_overrides_stale_error_metadata(tmp_path):
    python, script = _worker(tmp_path, '''
        import json
        print(json.dumps({'type':'provider.session','payload':{'session_id':'latest'}}), flush=True)
        print(json.dumps({'type':'error','payload':{'message':'HTTP 503','session_id':'stale'}}), flush=True)
        raise SystemExit(3)
    ''')
    with pytest.raises(ProviderError) as error:
        SDKRunner(worker_command=[python, str(script)]).run(ProviderRequest('claude', 'm', 'p', str(tmp_path)), lambda *_: None)
    assert error.value.session_id == 'latest'
