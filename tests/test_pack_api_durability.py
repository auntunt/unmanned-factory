"""API 层的耐久性回归：路由顺序、任务与作业的原子关联、重启恢复、取消、验证幂等。

走真实 API 与真实作业层；断言落在服务端留下的状态上，不是「某个 mock 被调用过」。
"""
import io
import time
import uuid

import pytest

from factory.control.capability_packs import PackStore
from tests.test_capability_packs import (TOOL_SOURCE, await_job, development_run, key,  # noqa: F401
                                         published_pack, selections, verified_pack)
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401

BASE = '/api/v4/capability-packs'


def bind_agent(client, headers, pack, version):
    aid = client.post('/api/v4/agents', json={'name': '造价助手', 'purpose': '清单'},
                      headers=headers).json()['id']
    bound = client.post(f'{BASE}/bindings', headers=headers,
                        json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    assert bound.status_code == 201, bound.text
    return aid


def invoke(client, headers, aid, pack, *, op=None, name='清单.csv'):
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_bytes()
    return client.post(f'{BASE}/invocations', headers=headers,
                       data={'agent_id': aid, 'pack_id': pack['id'], 'operation_key': op or key()},
                       files={'file': (name, io.BytesIO(source), 'text/csv')})


def settle(client, headers, task_id, *, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f'{BASE}/invocations/{task_id}', headers=headers).json()
        if task['status'] in ('succeeded', 'failed', 'cancelled'):
            return task
        time.sleep(0.1)
    raise AssertionError('任务未在超时前结束')


# ---- 路由顺序 ---------------------------------------------------------------
def test_static_paths_are_not_shadowed_by_the_pack_id_route(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    listing = client.get(f'{BASE}/invocations', headers=headers)
    assert listing.status_code == 200 and 'tasks' in listing.json()
    # 目录页与作业查询同样是静态段，不能被 /{pack_id} 吃掉。
    assert client.get(BASE, headers=headers).status_code == 200
    assert client.get(f'{BASE}/jobs/does-not-exist', headers=headers).status_code == 404
    assert client.get(f'{BASE}/bindings/no-such-agent', headers=headers).status_code == 200
    # 而真正的 pack id 仍然走详情路由。
    assert client.get(f'{BASE}/no-such-pack', headers=headers).status_code == 404


def test_listing_shows_the_caller_s_own_invocations(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    task_id = invoke(client, headers, aid, pack).json()['id']
    settle(client, headers, task_id)
    rows = client.get(f'{BASE}/invocations', headers=headers).json()['tasks']
    assert [row['id'] for row in rows] == [task_id]


# ---- 任务与作业的原子关联、重启恢复 -----------------------------------------
def test_task_carries_its_durable_job_id_from_creation(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    task = invoke(client, headers, aid, pack).json()
    assert task['job_id'], '任务没有在建表时就绑定作业 id'
    settled = settle(client, headers, task['id'])
    assert settled['job_id'] == task['job_id']
    job = client.get(f"{BASE}/jobs/{task['job_id']}", headers=headers)
    assert job.status_code == 200 and job.json()['status'] == 'completed'


def test_restart_moves_in_flight_tasks_to_an_explicit_terminal_state(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    packs = PackStore(store)
    actor = {'id': client.get('/api/auth/me', headers=headers).json()['user']['id'], 'role': 'admin'}
    # 崩在派发之前：任务已落库并带着 job id，但作业从未出现。
    stranded = packs.create_task(actor=actor, agent_id=aid, pack_id=pack['id'],
                                 input_artifact_ids=[], operation_key=key(),
                                 job_id=uuid.uuid4().hex)
    assert stranded['status'] == 'queued'
    recovered = packs.recover_interrupted(job_status=lambda jid: None)
    assert stranded['id'] in recovered
    after = client.get(f"{BASE}/invocations/{stranded['id']}", headers=headers).json()
    assert after['status'] == 'failed' and after['error_code'] == 'interrupted'
    assert '重新提交' in after['error']
    events = client.get(f"{BASE}/invocations/{stranded['id']}/events", headers=headers).json()
    assert 'task.interrupted' in [event['type'] for event in events['events']]
    # 已经结束的任务不会被恢复逻辑再动一次。
    assert packs.recover_interrupted(job_status=lambda jid: None) == []


def test_a_finished_task_is_left_alone_by_recovery(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    task_id = invoke(client, headers, aid, pack).json()['id']
    done = settle(client, headers, task_id)
    assert done['status'] == 'succeeded'
    assert PackStore(store).recover_interrupted(job_status=lambda jid: None) == []
    assert client.get(f'{BASE}/invocations/{task_id}', headers=headers).json()['status'] == 'succeeded'


def test_replay_redispatches_a_task_that_was_never_dispatched(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    packs = PackStore(store)
    actor = {'id': client.get('/api/auth/me', headers=headers).json()['user']['id'], 'role': 'admin'}
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_bytes()
    artifact = packs.put_artifact(actor_id=actor['id'], name='清单.csv', content=source,
                                  role='input', validation_status='not_applicable', dedupe=True)
    shared = key()
    stranded = packs.create_task(actor=actor, agent_id=aid, pack_id=pack['id'],
                                 input_artifact_ids=[artifact['id']], operation_key=shared,
                                 job_id=uuid.uuid4().hex)
    # 重试同一个操作键：不是新建第二个任务，而是把这个从未派发的任务真正跑起来。
    retry = invoke(client, headers, aid, pack, op=shared)
    assert retry.status_code == 202 and retry.json()['id'] == stranded['id']
    finished = settle(client, headers, stranded['id'])
    assert finished['status'] == 'succeeded'
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM pack_tasks').fetchone()[0] == 1


def test_replay_of_a_finished_task_does_not_convert_the_file_again(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    shared = key()
    first = invoke(client, headers, aid, pack, op=shared).json()
    done = settle(client, headers, first['id'])
    outputs = [item['id'] for item in done['outputs']]
    again = invoke(client, headers, aid, pack, op=shared)
    assert again.status_code == 202 and again.json()['id'] == first['id']
    assert [item['id'] for item in again.json()['outputs']] == outputs  # 没有重跑
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pack_tasks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM maintenance_jobs WHERE conversation_id=?",
                          (f"pack-task:{first['id']}",)).fetchone()[0] == 1


# ---- 取消 -------------------------------------------------------------------
def test_cancel_is_persisted_and_reaches_the_job(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    packs = PackStore(store)
    actor = {'id': client.get('/api/auth/me', headers=headers).json()['user']['id'], 'role': 'admin'}
    task = packs.create_task(actor=actor, agent_id=aid, pack_id=pack['id'], input_artifact_ids=[],
                             operation_key=key(), job_id=uuid.uuid4().hex)
    cancelled = client.post(f"{BASE}/invocations/{task['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()['status'] == 'cancel_requested'
    # 取消意图落了库：重启恢复看到的是取消，而不是「还在跑」。
    stored = client.get(f"{BASE}/invocations/{task['id']}", headers=headers).json()
    assert stored['status'] == 'cancel_requested'
    events = client.get(f"{BASE}/invocations/{task['id']}/events", headers=headers).json()
    assert 'task.cancel_requested' in [event['type'] for event in events['events']]


def test_cancelling_a_finished_task_is_a_no_op(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    task_id = invoke(client, headers, aid, pack).json()['id']
    settle(client, headers, task_id)
    response = client.post(f'{BASE}/invocations/{task_id}/cancel', headers=headers)
    assert response.status_code == 200 and response.json()['status'] == 'succeeded'


def test_another_user_cannot_cancel_or_read_someone_elses_task(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    task_id = invoke(client, headers, aid, pack).json()['id']
    settle(client, headers, task_id)
    client.app.state.auth.create_user('member2', 'another-long-password', role='member')
    other = client.post('/api/auth/login', json={'username': 'member2', 'password': 'another-long-password'},
                        headers={'Origin': 'http://testserver'})
    member = {'Origin': 'http://testserver', 'X-CSRF-Token': other.json()['csrf_token']}
    assert client.get(f'{BASE}/invocations/{task_id}', headers=member).status_code == 403
    assert client.post(f'{BASE}/invocations/{task_id}/cancel', headers=member).status_code == 403


# ---- 验证提交的幂等 ---------------------------------------------------------
def test_repeating_an_evaluation_key_runs_the_test_set_once(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    rid = development_run(client, store, repo, headers)
    pack = client.post(f'{BASE}/drafts', headers=headers,
                       json={'source_run_id': rid, 'selections': selections(),
                             'operation_key': key()}).json()
    shared, revision = key(), pack['draft']['revision']
    first = client.post(f"{BASE}/{pack['id']}/evaluations", headers=headers,
                        json={'expected_revision': revision, 'operation_key': shared})
    assert first.status_code == 202
    second = client.post(f"{BASE}/{pack['id']}/evaluations", headers=headers,
                         json={'expected_revision': revision, 'operation_key': shared})
    assert second.status_code == 202
    assert second.json()['idempotent_replay'] is True
    assert second.json()['job_id'] == first.json()['job_id']
    await_job(client, headers, first.json()['job_id'])
    # 真实执行次数：一个作业、一条证据。
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM maintenance_jobs WHERE conversation_id=?',
                          (f"pack-eval:{pack['id']}",)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM pack_evaluations WHERE pack_id=?',
                          (pack['id'],)).fetchone()[0] == 1
    third = client.post(f"{BASE}/{pack['id']}/evaluations", headers=headers,
                        json={'expected_revision': revision, 'operation_key': shared})
    assert third.status_code == 202 and third.json()['idempotent_replay'] is True
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM pack_evaluations WHERE pack_id=?',
                          (pack['id'],)).fetchone()[0] == 1


def test_same_evaluation_key_on_different_content_is_a_conflict(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, _ = verified_pack(client, store, repo, headers)
    detail = client.get(f"{BASE}/{pack['id']}", headers=headers).json()
    reused = [record for record in detail['evaluations']]
    assert reused, '前置验证没有留下证据'
    files = [{'path': item['path'], 'content': (TOOL_SOURCE / item['path']).read_text(),
              'role': item['role'], 'material_scope': item.get('material_scope')}
             for item in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'README.md':
            entry['content'] += '\n改一点内容让摘要变化。\n'
    revision = client.put(f"{BASE}/{pack['id']}/draft", headers=headers,
                          json={'expected_revision': detail['draft']['revision'], 'files': files}
                          ).json()['draft']['revision']
    # 复用前一次验证用过的操作键，但候选内容已经变了 → 必须冲突，不能静默复用旧证据。
    with store.connect() as db:
        used_key = db.execute('SELECT operation_key FROM pack_operations ORDER BY rowid DESC').fetchall()
    keys = [row['operation_key'] for row in used_key]
    clash = client.post(f"{BASE}/{pack['id']}/evaluations", headers=headers,
                        json={'expected_revision': revision, 'operation_key': keys[-1]})
    assert clash.status_code == 409

# ---- 环境检查会过期 ---------------------------------------------------------
def test_environment_checks_expire_instead_of_staying_green_forever(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    detail = client.get(f"{BASE}/{pack['id']}", headers=headers).json()
    assert detail['environments'][version['id']]['status'] in ('ready', 'unavailable')

    packs = PackStore(store)
    # 运行环境换了：上一次的 ready 不能继续代表「现在能跑」。
    import json as _json
    with store.connect() as db:
        row = db.execute('SELECT data FROM pack_env_checks WHERE version_id=?', (version['id'],)).fetchone()
        body = _json.loads(row['data'])
        body['runtime_fingerprint'] = 'python-from-another-machine'
        db.execute('UPDATE pack_env_checks SET data=? WHERE version_id=?',
                   (_json.dumps(body, ensure_ascii=False), version['id']))
    stale = client.get(f"{BASE}/{pack['id']}", headers=headers).json()['environments'][version['id']]
    assert stale['status'] == 'unchecked' and stale['stale'] is True
    assert '运行环境已变化' in stale['stale_reason']

    # 重新检查后恢复可用，并且发布状态自始至终没有被改动。
    rechecked = client.post(f"{BASE}/versions/{version['id']}/environment-check", headers=headers)
    assert rechecked.status_code == 200
    after = client.get(f"{BASE}/{pack['id']}", headers=headers).json()
    assert after['environments'][version['id']]['stale'] is False
    assert after['versions'][0]['lifecycle'] == 'published'


def test_the_role_version_is_frozen_inside_the_task_transaction(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = bind_agent(client, headers, pack, version)
    task = invoke(client, headers, aid, pack).json()
    assert task['snapshot']['agent_version'] == 1
    # 角色升级之后，既有任务的快照不变。
    agent = client.get(f'/api/v4/agents/{aid}', headers=headers).json()
    client.post(f'/api/v4/agents/{aid}/draft', headers=headers,
                json={'expected_revision': agent['draft']['revision'],
                      'patch': {'instructions': '更新后的方法说明'}})
    settle(client, headers, task['id'])
    assert client.get(f"{BASE}/invocations/{task['id']}", headers=headers
                      ).json()['snapshot']['agent_version'] == 1

def test_starting_the_service_never_changes_the_process_limits(app_env):
    """服务启动（建 app、装路由、跑恢复）不得碰服务进程的 rlimit。"""
    import resource
    client, store, service, repo = app_env
    watched = [name for name in ('RLIMIT_AS', 'RLIMIT_DATA', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE')
               if getattr(resource, name, None) is not None]
    before = {name: resource.getrlimit(getattr(resource, name)) for name in watched}
    headers = login(client)
    # 真的用一次职能包路由，确保模块与探测都被触发过。
    assert client.get(f'{BASE}/invocations', headers=headers).status_code == 200
    from factory.control.pack_routes import router
    router(store, service)  # 再建一次路由：恢复逻辑会跑，仍然不许动限制
    after = {name: resource.getrlimit(getattr(resource, name)) for name in watched}
    assert after == before, f'服务进程的 rlimit 被改动了：{before} -> {after}'
