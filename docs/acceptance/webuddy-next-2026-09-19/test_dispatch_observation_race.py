import threading
from concurrent.futures import ThreadPoolExecutor
from tests.test_admin_config_conversation import env, _login, _wait_job
from factory.control.providers import ProviderResult


def test_stale_missing_job_observation_must_not_interrupt_registered_job(env, monkeypatch):
    client, store, service = env
    h = _login(client)
    cid = client.post('/api/v4/admin-config/conversations', headers=h, json={}).json()['id']
    dispatch_entered = threading.Event()
    allow_register = threading.Event()
    missing_observed = threading.Event()
    post_finished = threading.Event()
    allow_answer = threading.Event()
    real_start = service.start_maintenance
    real_status = service.maintenance_status
    def start(fn, **kw):
        dispatch_entered.set()
        assert allow_register.wait(5)
        return real_start(fn, **kw)
    def status(jid):
        try:
            return real_status(jid)
        except KeyError:
            missing_observed.set()
            assert post_finished.wait(5)
            raise
    def run(req, emit, cancel):
        assert allow_answer.wait(5)
        return ProviderResult('REAL_ANSWER_AFTER_REGISTRATION')
    monkeypatch.setattr(service, 'start_maintenance', start)
    monkeypatch.setattr(service, 'maintenance_status', status)
    monkeypatch.setattr(service.runner, 'run', run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            post = pool.submit(client.post, f'/api/v4/admin-config/conversations/{cid}/messages', headers=h, json={'content':'Read configuration'})
            assert dispatch_entered.wait(5)
            get = pool.submit(client.get, f'/api/v4/admin-config/conversations/{cid}', headers=h)
            assert missing_observed.wait(5)
            allow_register.set()
            sent = post.result(timeout=5)
            assert sent.status_code == 201
            jid = sent.json()['job_id']
            assert real_status(jid)['status'] in ('pending','running')
            post_finished.set()
            observed = get.result(timeout=5).json()
            monkeypatch.setattr(service, 'maintenance_status', real_status)
            allow_answer.set()
            assert _wait_job(service,jid)['status'] == 'completed'
            final = client.get(f'/api/v4/admin-config/conversations/{cid}',headers=h).json()
            msg = next(m for m in final['messages'] if m.get('job_id')==jid)
            assert msg['status']=='completed' and msg['content']=='REAL_ANSWER_AFTER_REGISTRATION', {'during_poll':observed,'final_message':msg}
        finally:
            allow_register.set()
            post_finished.set()
            allow_answer.set()
