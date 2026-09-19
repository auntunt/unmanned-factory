import threading
import time
from tests.test_admin_config_conversation import env, _login
from factory.control.providers import ProviderResult


def test_poll_must_not_overwrite_completed_answer(env, monkeypatch):
    client, store, service = env
    h = _login(client)
    gate = threading.Event()
    def run(req, emit, cancel):
        assert gate.wait(5)
        return ProviderResult('FINAL_ANSWER_MUST_SURVIVE_POLL')
    monkeypatch.setattr(service.runner, 'run', run)
    cid = client.post('/api/v4/admin-config/conversations', headers=h, json={}).json()['id']
    response = client.post(f'/api/v4/admin-config/conversations/{cid}/messages', headers=h, json={'content': 'Read current configuration'})
    assert response.status_code == 201
    jid = response.json()['job_id']
    original_status = service.maintenance_status
    # GET has read the pending conversation. Let the real job finish and save
    # its answer before GET's reconciliation observes its terminal status.
    def finish_between_read_and_reconcile(job_id):
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = original_status(job_id)
            if state['status'] == 'completed':
                return state
            time.sleep(.01)
        raise AssertionError('job did not complete')
    monkeypatch.setattr(service, 'maintenance_status', finish_between_read_and_reconcile)
    result = client.get(f'/api/v4/admin-config/conversations/{cid}', headers=h)
    assert result.status_code == 200
    monkeypatch.setattr(service, 'maintenance_status', original_status)
    assert original_status(jid)['status'] == 'completed'
    persisted = client.get(f'/api/v4/admin-config/conversations/{cid}', headers=h).json()
    answer = next(m for m in persisted['messages'] if m.get('job_id') == jid)
    assert answer['content'] == 'FINAL_ANSWER_MUST_SURVIVE_POLL', answer
