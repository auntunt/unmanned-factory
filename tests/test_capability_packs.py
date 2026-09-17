"""Capability pack lifecycle: development output → candidate → real evaluation →
immutable published version → agent binding → isolated invocation → download.

Every assertion here goes through the real store, the real API and the real subprocess
runtime. Nothing asserts that a mock was called: a "converted" file is compared byte for
byte against the expected output, and a refusal is asserted by the state the server is
actually left in.
"""
import io
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from factory.control.capability_packs import PackStore
from factory.control.deliverables import snapshot
from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401

TOOL_SOURCE = Path(__file__).resolve().parents[1] / 'factory/control/builtin_packs/tools/csv-quote-xml'
PROGRAM = ['webuddy-pack.json', 'README.md', 'tool/main.py', 'tool/quote_xml.py']
FIXTURES = ['fixtures/tests.json', 'fixtures/sample_quote.csv', 'fixtures/missing_column.csv',
            'fixtures/expected_quote.xml']


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True).stdout.decode().strip()


def development_run(client, store, repo, headers, *, extra=None):
    """A real development run that produced the tool, with its deliverables saved."""
    pid = client.post('/api/v2/projects', json={'name': '转换能力', 'repository': 'owner/sample',
                                                'budget_usd': 10, 'workspace': str(repo)},
                      headers=headers).json()['id']
    for name in PROGRAM + FIXTURES:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TOOL_SOURCE / name, target)
    for name, content in (extra or {}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'csv→xml 转换器')
    run, _ = store.create_run(pid, '开发 CSV 工程量清单转 XML 的能力')
    store.update(run['id'], {'status': 'ready_for_review',
                             'artifacts': {'worktree': str(repo), 'commit': git(repo, 'rev-parse', 'HEAD')}})
    snapshot(store, store.get(run['id']))
    return run['id']


def selections(names=None, *, fixtures=True):
    rows = [{'name': n} for n in (names or PROGRAM)]
    if fixtures:
        rows += [{'name': n, 'material_scope': 'synthetic'} for n in FIXTURES]
    return rows


def key():
    return uuid.uuid4().hex


def await_job(client, headers, job_id, *, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f'/api/v4/capability-packs/jobs/{job_id}', headers=headers).json()
        if job['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
            return job
        time.sleep(0.1)
    raise AssertionError('作业未在超时前结束')


def verified_pack(client, store, repo, headers, **kwargs):
    rid = development_run(client, store, repo, headers, **kwargs)
    created = client.post('/api/v4/capability-packs/drafts', headers=headers,
                          json={'source_run_id': rid, 'selections': selections(), 'operation_key': key()})
    assert created.status_code == 201, created.text
    pack = created.json()
    started = client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations", headers=headers,
                          json={'expected_revision': pack['draft']['revision'], 'operation_key': key()})
    assert started.status_code == 202, started.text
    job = await_job(client, headers, started.json()['job_id'])
    assert job['status'] == 'completed', job
    return pack, job['result']


def published_pack(client, store, repo, headers, **kwargs):
    pack, result = verified_pack(client, store, repo, headers, **kwargs)
    assert result['passed'], result
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    version = client.post(f"/api/v4/capability-packs/{pack['id']}/versions", headers=headers,
                          json={'expected_revision': detail['draft']['revision'],
                                'evaluation_id': result['evaluation_id'], 'operation_key': key()})
    assert version.status_code == 201, version.text
    return pack, version.json()


# ---- B. the vertical slice ------------------------------------------------
def test_development_output_to_published_capability_and_real_download(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    assert version['version'] == 1 and len(version['content_digest']) == 64

    aid = client.post('/api/v4/agents', json={'name': '造价助手', 'purpose': '清单与报价'},
                      headers=headers).json()['id']
    bound = client.post('/api/v4/capability-packs/bindings', headers=headers,
                        json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    assert bound.status_code == 201 and bound.json()['version'] == 1

    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_bytes()
    started = client.post('/api/v4/capability-packs/invocations', headers=headers,
                          data={'agent_id': aid, 'pack_id': pack['id'], 'operation_key': key()},
                          files={'file': ('清单.csv', io.BytesIO(source), 'text/csv')})
    assert started.status_code == 202, started.text
    task_id = started.json()['id']
    deadline = time.time() + 90
    while time.time() < deadline:
        task = client.get(f'/api/v4/capability-packs/invocations/{task_id}', headers=headers).json()
        if task['status'] in ('succeeded', 'failed', 'cancelled'):
            break
        time.sleep(0.1)
    assert task['status'] == 'succeeded', task
    assert task['validation_status'] == 'passed'
    assert task['result']['total'] == '18920.70' and task['result']['item_count'] == 3
    assert task['snapshot']['version'] == 1 and task['snapshot']['content_digest'] == version['content_digest']
    # The role's version is frozen alongside the capability's, so a later change to either
    # cannot silently redefine what this task ran.
    assert task['snapshot']['agent_version'] == 1

    output = task['outputs'][0]
    download = client.get(f"/api/v4/capability-packs/artifacts/{output['id']}/download", headers=headers)
    assert download.status_code == 200
    # The delivered bytes are the real conversion, not a fixture copied through: every
    # <Item> line matches the expected output byte for byte, and the total is recomputed.
    expected = (TOOL_SOURCE / 'fixtures/expected_quote.xml').read_text(encoding='utf-8')
    produced = download.content.decode('utf-8')
    items = [line for line in expected.splitlines() if line.strip().startswith('<Item')]
    assert [line for line in produced.splitlines() if line.strip().startswith('<Item')] == items
    assert 'total="18920.70"' in produced and 'itemCount="3"' in produced
    assert download.headers['x-validation-status'] == 'passed'

    events = client.get(f'/api/v4/capability-packs/invocations/{task_id}/events', headers=headers).json()
    assert [e['type'] for e in events['events']][:2] == ['task.queued', 'task.running']
    # Resumable by cursor: a refresh re-reads from where it stopped, it does not re-run.
    resumed = client.get(f"/api/v4/capability-packs/invocations/{task_id}/events?cursor={events['events'][0]['id']}",
                         headers=headers).json()
    assert [e['type'] for e in resumed['events']] == [e['type'] for e in events['events'][1:]]


# ---- A. what may enter a pack --------------------------------------------
def test_client_material_and_task_results_never_enter_the_pack(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    rid = development_run(client, store, repo, headers, extra={
        'input/客户清单.csv': '编号,名称\nX,客户私有数据\n',
        'report/交付成果.xml': '<Quote/>\n',
        'fixtures/undeclared.csv': '编号,名称,单位,数量,单价\n',
    })
    created = client.post('/api/v4/capability-packs/drafts', headers=headers, json={
        'source_run_id': rid, 'operation_key': key(),
        'selections': selections() + [{'name': 'input/客户清单.csv'}, {'name': 'report/交付成果.xml'},
                                      {'name': 'fixtures/undeclared.csv'}]})
    assert created.status_code == 201, created.text
    excluded = {row['path']: row['reason'] for row in created.json()['draft']['excluded']}
    assert '客户原始资料默认不入包' in excluded['input/客户清单.csv']
    assert '任务结果默认不入包' in excluded['report/交付成果.xml']
    assert '资料范围' in excluded['fixtures/undeclared.csv']
    kept = {f['path'] for f in created.json()['draft']['files']}
    assert 'input/客户清单.csv' not in kept and 'report/交付成果.xml' not in kept
    assert 'tool/main.py' in kept and 'fixtures/sample_quote.csv' in kept


def test_missing_tool_contract_blocks_validation_and_publish(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    rid = development_run(client, store, repo, headers)
    created = client.post('/api/v4/capability-packs/drafts', headers=headers, json={
        'source_run_id': rid, 'operation_key': key(),
        'selections': [{'name': 'tool/main.py'}, {'name': 'tool/quote_xml.py'}]})
    draft = created.json()['draft']
    assert draft['lifecycle'] == 'needs_changes' and 'webuddy-pack.json' in draft['blocked_reason']
    blocked = client.post(f"/api/v4/capability-packs/{created.json()['id']}/evaluations", headers=headers,
                          json={'expected_revision': draft['revision'], 'operation_key': key()})
    assert blocked.status_code == 409


def test_contract_requiring_network_is_refused(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, _ = verified_pack(client, store, repo, headers)
    manifest = json.loads((TOOL_SOURCE / 'webuddy-pack.json').read_text())
    manifest['tool']['permissions']['network'] = True
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    files = [{'path': f['path'], 'content': (TOOL_SOURCE / f['path']).read_text(),
              'role': f['role'], 'material_scope': f.get('material_scope')} for f in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'webuddy-pack.json':
            entry['content'] = json.dumps(manifest, ensure_ascii=False)
    updated = client.put(f"/api/v4/capability-packs/{pack['id']}/draft", headers=headers,
                         json={'expected_revision': detail['draft']['revision'], 'files': files})
    assert updated.status_code == 200
    assert '不联网' in updated.json()['draft']['blocked_reason']
    assert updated.json()['draft']['lifecycle'] == 'needs_changes'


# ---- publish conditions ---------------------------------------------------
def test_candidate_modified_after_validation_cannot_publish_on_old_evidence(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, result = verified_pack(client, store, repo, headers)
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    files = [{'path': f['path'], 'content': (TOOL_SOURCE / f['path']).read_text(),
              'role': f['role'], 'material_scope': f.get('material_scope')} for f in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'tool/quote_xml.py':
            entry['content'] += '\n# 验证之后偷偷改了程序\n'
    updated = client.put(f"/api/v4/capability-packs/{pack['id']}/draft", headers=headers,
                         json={'expected_revision': detail['draft']['revision'], 'files': files})
    assert updated.status_code == 200
    assert updated.json()['draft']['content_digest'] != detail['draft']['content_digest']
    refused = client.post(f"/api/v4/capability-packs/{pack['id']}/versions", headers=headers,
                          json={'expected_revision': updated.json()['draft']['revision'],
                                'evaluation_id': result['evaluation_id'], 'operation_key': key()})
    assert refused.status_code == 409 and '已被修改' in refused.text
    after = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    assert after['versions'] == []


def test_failed_evaluation_cannot_publish(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, _ = verified_pack(client, store, repo, headers)
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    files = [{'path': f['path'], 'content': (TOOL_SOURCE / f['path']).read_text(),
              'role': f['role'], 'material_scope': f.get('material_scope')} for f in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'tool/quote_xml.py':
            entry['content'] = entry['content'].replace("Decimal('0.01')", "Decimal('1')")
    revision = client.put(f"/api/v4/capability-packs/{pack['id']}/draft", headers=headers,
                          json={'expected_revision': detail['draft']['revision'], 'files': files}
                          ).json()['draft']['revision']
    started = client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations", headers=headers,
                          json={'expected_revision': revision, 'operation_key': key()})
    job = await_job(client, headers, started.json()['job_id'])
    assert job['result']['passed'] is False
    refused = client.post(f"/api/v4/capability-packs/{pack['id']}/versions", headers=headers,
                          json={'expected_revision': revision, 'evaluation_id': job['result']['evaluation_id'],
                                'operation_key': key()})
    assert refused.status_code == 409 and '不能发布' in refused.text


def test_concurrent_publish_creates_exactly_one_version(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, result = verified_pack(client, store, repo, headers)
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    packs = PackStore(store)
    actor = {'id': client.get('/api/auth/me', headers=headers).json()['user']['id'], 'role': 'admin'}
    barrier = threading.Barrier(6)
    created, refused = [], []

    def publish():
        barrier.wait()
        try:
            created.append(packs.publish(pack['id'], expected_revision=detail['draft']['revision'],
                                         evaluation_id=result['evaluation_id'], actor=actor,
                                         operation_key=f'publish-{threading.get_ident()}')['version'])
        except Exception as exc:
            refused.append(type(exc).__name__)

    threads = [threading.Thread(target=publish) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert created == [1], (created, refused)
    assert len(refused) == 5
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM pack_versions WHERE pack_id=?', (pack['id'],)).fetchone()[0] == 1


def test_published_version_rows_are_immutable(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    import sqlite3
    with store.connect() as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE pack_versions SET content_digest='0' WHERE id=?", (version['id'],))
        with pytest.raises(sqlite3.DatabaseError):
            db.execute('DELETE FROM pack_versions WHERE id=?', (version['id'],))


# ---- idempotency and lost responses --------------------------------------
def test_lost_response_retry_never_duplicates_draft_or_version_or_task(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    rid = development_run(client, store, repo, headers)
    draft_key = key()
    first = client.post('/api/v4/capability-packs/drafts', headers=headers,
                        json={'source_run_id': rid, 'selections': selections(), 'operation_key': draft_key})
    again = client.post('/api/v4/capability-packs/drafts', headers=headers,
                        json={'source_run_id': rid, 'selections': selections(), 'operation_key': draft_key})
    assert first.json()['id'] == again.json()['id']
    assert len(client.get('/api/v4/capability-packs', headers=headers).json()['packs']) == 1

    pack = first.json()
    started = client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations", headers=headers,
                          json={'expected_revision': pack['draft']['revision'], 'operation_key': key()})
    result = await_job(client, headers, started.json()['job_id'])['result']
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    publish_key = key()
    payload = {'expected_revision': detail['draft']['revision'], 'evaluation_id': result['evaluation_id'],
               'operation_key': publish_key}
    v1 = client.post(f"/api/v4/capability-packs/{pack['id']}/versions", headers=headers, json=payload)
    v2 = client.post(f"/api/v4/capability-packs/{pack['id']}/versions", headers=headers, json=payload)
    assert v1.status_code == 201 and v2.status_code == 201 and v1.json()['id'] == v2.json()['id']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM pack_versions').fetchone()[0] == 1

    aid = client.post('/api/v4/agents', json={'name': '造价助手', 'purpose': '清单'},
                      headers=headers).json()['id']
    client.post('/api/v4/capability-packs/bindings', headers=headers,
                json={'agent_id': aid, 'version_id': v1.json()['id'], 'expected_revision': 0})
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_bytes()
    task_key = key()
    ids = set()
    for _ in range(2):
        response = client.post('/api/v4/capability-packs/invocations', headers=headers,
                               data={'agent_id': aid, 'pack_id': pack['id'], 'operation_key': task_key},
                               files={'file': ('清单.csv', io.BytesIO(source), 'text/csv')})
        assert response.status_code == 202, response.text
        ids.add(response.json()['id'])
    assert len(ids) == 1
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM pack_tasks').fetchone()[0] == 1


def test_same_operation_key_with_different_input_is_a_conflict(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    rid = development_run(client, store, repo, headers)
    shared = key()
    assert client.post('/api/v4/capability-packs/drafts', headers=headers,
                       json={'source_run_id': rid, 'selections': selections(),
                             'operation_key': shared}).status_code == 201
    clash = client.post('/api/v4/capability-packs/drafts', headers=headers,
                        json={'source_run_id': rid, 'selections': selections(['tool/main.py', 'webuddy-pack.json']),
                              'operation_key': shared})
    assert clash.status_code == 409


# ---- versions, bindings and frozen tasks ---------------------------------
def test_upgrading_a_binding_does_not_change_an_existing_task_snapshot(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': '造价助手', 'purpose': '清单'},
                      headers=headers).json()['id']
    client.post('/api/v4/capability-packs/bindings', headers=headers,
                json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_bytes()
    task_id = client.post('/api/v4/capability-packs/invocations', headers=headers,
                          data={'agent_id': aid, 'pack_id': pack['id'], 'operation_key': key()},
                          files={'file': ('清单.csv', io.BytesIO(source), 'text/csv')}).json()['id']

    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    files = [{'path': f['path'], 'content': (TOOL_SOURCE / f['path']).read_text(),
              'role': f['role'], 'material_scope': f.get('material_scope')} for f in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'README.md':
            entry['content'] += '\n第二版说明。\n'
    revision = client.put(f"/api/v4/capability-packs/{pack['id']}/draft", headers=headers,
                          json={'expected_revision': detail['draft']['revision'], 'files': files}
                          ).json()['draft']['revision']
    started = client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations", headers=headers,
                          json={'expected_revision': revision, 'operation_key': key()})
    result = await_job(client, headers, started.json()['job_id'])['result']
    assert result['passed']
    v2 = client.post(f"/api/v4/capability-packs/{pack['id']}/versions", headers=headers,
                     json={'expected_revision': revision, 'evaluation_id': result['evaluation_id'],
                           'operation_key': key()}).json()
    assert v2['version'] == 2

    bindings = client.get(f'/api/v4/capability-packs/bindings/{aid}', headers=headers).json()['bindings']
    assert bindings[0]['version'] == 1 and bindings[0]['upgrade_available'] is True  # never a silent upgrade
    client.post('/api/v4/capability-packs/bindings', headers=headers,
                json={'agent_id': aid, 'version_id': v2['id'], 'expected_revision': bindings[0]['revision']})
    task = client.get(f'/api/v4/capability-packs/invocations/{task_id}', headers=headers).json()
    assert task['snapshot']['version'] == 1
    assert task['snapshot']['content_digest'] == version['content_digest']
    # Rolling back points the binding at the old version; version 2 still exists.
    rolled = client.post('/api/v4/capability-packs/bindings', headers=headers,
                         json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 2}).json()
    assert rolled['version'] == 1
    assert len(client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()['versions']) == 2


def test_stale_binding_revision_is_refused(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': '造价助手', 'purpose': '清单'},
                      headers=headers).json()['id']
    client.post('/api/v4/capability-packs/bindings', headers=headers,
                json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    stale = client.post('/api/v4/capability-packs/bindings', headers=headers,
                        json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    assert stale.status_code == 409


def test_invocation_requires_a_binding(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': '没有挂靠的角色', 'purpose': ''},
                      headers=headers).json()['id']
    source = (TOOL_SOURCE / 'fixtures/sample_quote.csv').read_bytes()
    refused = client.post('/api/v4/capability-packs/invocations', headers=headers,
                          data={'agent_id': aid, 'pack_id': pack['id'], 'operation_key': key()},
                          files={'file': ('清单.csv', io.BytesIO(source), 'text/csv')})
    assert refused.status_code == 409 and '尚未挂靠' in refused.text


# ---- isolation and honest results ----------------------------------------
def test_environment_unavailable_does_not_unpublish_the_version(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    packs = PackStore(store)
    packs.record_env_check(version['id'], {'status': 'unavailable', 'missing': ['lxml'],
                                           'python': '3', 'platform': 'test', 'sandbox': 'none'})
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    assert detail['versions'][0]['lifecycle'] == 'published'  # still published
    assert detail['environments'][version['id']]['status'] == 'unavailable'
    assert detail['environments'][version['id']]['missing'] == ['lxml']


def test_cross_user_artifact_reference_is_refused(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    aid = client.post('/api/v4/agents', json={'name': '造价助手', 'purpose': '清单'},
                      headers=headers).json()['id']
    client.post('/api/v4/capability-packs/bindings', headers=headers,
                json={'agent_id': aid, 'version_id': version['id'], 'expected_revision': 0})
    packs = PackStore(store)
    other = packs.put_artifact(actor_id=99999, name='别人的清单.csv', content=b'\xe7\xa7\x81\xe6\x9c\x89',
                               role='input')
    owner = {'id': client.get('/api/auth/me', headers=headers).json()['user']['id'], 'role': 'member'}
    with pytest.raises(PermissionError):
        packs.create_task(actor=owner, agent_id=aid, pack_id=pack['id'],
                          input_artifact_ids=[other['id']], operation_key=key())
    with pytest.raises(PermissionError):
        packs.artifact(other['id'], actor=owner)
    # And over the real API as a non-admin member, who must not read another user's file.
    client.app.state.auth.create_user('member1', 'another-long-password', role='member')
    member = client.post('/api/auth/login', json={'username': 'member1', 'password': 'another-long-password'},
                         headers={'Origin': 'http://testserver'})
    member_headers = {'Origin': 'http://testserver', 'X-CSRF-Token': member.json()['csrf_token']}
    assert client.get(f"/api/v4/capability-packs/artifacts/{other['id']}/download",
                      headers=member_headers).status_code == 403


@pytest.mark.parametrize(('body', 'expected_code', 'note'), [
    ("import json,sys; json.load(sys.stdin); print(json.dumps({'status':'ok','outputs':[]}))",
     'validation_failed', '声称成功但没有产出'),
    ("import json,sys; json.load(sys.stdin); print(json.dumps({'status':'ok','outputs':[{'path':'../escape.xml'}]}))",
     'validation_failed', '越界路径'),
    ("import json,sys,time; json.load(sys.stdin); time.sleep(30)", 'timeout', '超时'),
    ("import json,sys; json.load(sys.stdin); print('这不是 JSON')", 'bad_contract', '不按契约输出'),
    ("import sys; sys.exit(3)", 'tool_error', '工具崩溃'),
])
def test_dishonest_or_broken_tools_fail_with_a_structured_reason(app_env, body, expected_code, note):
    client, store, service, repo = app_env
    headers = login(client)
    pack, _ = verified_pack(client, store, repo, headers)
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    manifest = json.loads((TOOL_SOURCE / 'webuddy-pack.json').read_text())
    manifest['tool']['timeout_seconds'] = 2
    files = [{'path': f['path'], 'content': (TOOL_SOURCE / f['path']).read_text(),
              'role': f['role'], 'material_scope': f.get('material_scope')} for f in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'tool/main.py':
            entry['content'] = body
        if entry['path'] == 'webuddy-pack.json':
            entry['content'] = json.dumps(manifest, ensure_ascii=False)
    revision = client.put(f"/api/v4/capability-packs/{pack['id']}/draft", headers=headers,
                          json={'expected_revision': detail['draft']['revision'], 'files': files}
                          ).json()['draft']['revision']
    started = client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations", headers=headers,
                          json={'expected_revision': revision, 'operation_key': key()})
    result = await_job(client, headers, started.json()['job_id'])['result']
    assert result['passed'] is False, note
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    case = detail['evaluations'][0]['cases'][0]
    assert case['error_code'] == expected_code, (note, case)
    assert detail['draft']['lifecycle'] == 'needs_changes'


def test_tool_cannot_write_outside_its_output_directory(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, _ = verified_pack(client, store, repo, headers)
    detail = client.get(f"/api/v4/capability-packs/{pack['id']}", headers=headers).json()
    escape_target = repo.parent / 'escaped.txt'
    body = ('import json,sys\n'
            'json.load(sys.stdin)\n'
            'try:\n'
            f"    open({str(escape_target)!r}, 'w').write('escaped')\n"
            "    print(json.dumps({'status':'ok','outputs':[],'result':{'escaped':True}}))\n"
            'except Exception as exc:\n'
            "    print(json.dumps({'status':'error','error_code':'write_denied','error':str(exc),'outputs':[]}))\n")
    files = [{'path': f['path'], 'content': (TOOL_SOURCE / f['path']).read_text(),
              'role': f['role'], 'material_scope': f.get('material_scope')} for f in detail['draft']['files']]
    for entry in files:
        if entry['path'] == 'tool/main.py':
            entry['content'] = body
    revision = client.put(f"/api/v4/capability-packs/{pack['id']}/draft", headers=headers,
                          json={'expected_revision': detail['draft']['revision'], 'files': files}
                          ).json()['draft']['revision']
    started = client.post(f"/api/v4/capability-packs/{pack['id']}/evaluations", headers=headers,
                          json={'expected_revision': revision, 'operation_key': key()})
    result = await_job(client, headers, started.json()['job_id'])['result']
    assert result['passed'] is False
    evaluation = client.get(f"/api/v4/capability-packs/{pack['id']}",
                            headers=headers).json()['evaluations'][0]
    isolation = evaluation['cases'][0]['isolation']
    if isolation == 'seatbelt':
        assert not escape_target.exists(), '沙箱可用时必须真的挡住越界写'
    else:
        # No sandbox on this host: we say so rather than claiming confinement.
        assert evaluation['environment']['sandbox'] == 'none'


def test_maintainer_only_operations_are_enforced_server_side(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    pack, version = published_pack(client, store, repo, headers)
    packs = PackStore(store)
    stranger = {'id': 987654, 'role': 'member'}
    with pytest.raises(PermissionError):
        packs.update_draft(pack['id'], expected_revision=2, files=[{'path': 'a.py', 'content': 'x'}], actor=stranger)
    with pytest.raises(PermissionError):
        packs.publish(pack['id'], expected_revision=2, evaluation_id='x', actor=stranger)
    with pytest.raises(PermissionError):
        packs.bind('agent', version['id'], expected_revision=0, actor=stranger)
