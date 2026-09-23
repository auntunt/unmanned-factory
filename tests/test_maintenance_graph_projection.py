"""Relation canvas projection: real stages, honest gaps, and the caller's scope only.

Synthetic repositories and the FakeSDK executor (no paid model).
"""
import subprocess

from tests.test_control_app import app_env, login, wait_state  # noqa: F401
from tests.test_maintenance_subsystem import _adopt_greeting_check, _deliver, _member, _register


def _graph(client, **params):
    res = client.get('/api/v2/maintenance/graph', params=params)
    assert res.status_code == 200, res.text
    body = res.json()
    return body, {n['id']: n for n in body['nodes']}, {(e['from'], e['to']): e['kind'] for e in body['edges']}


def _facts(node):
    return {f['label']: f['value'] for f in node['facts']}


def _submit(client, pid, headers, key):
    res = client.post('/api/v2/maintenance/requirements', json={
        'project_id': pid, 'content': '把问候语改成 hello world', 'idempotency_key': key}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def test_chain_follows_the_real_stages_and_marks_what_is_not_recorded(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = _register(client, repo, headers)['project_id']
    _adopt_greeting_check(store, pid)
    receipt = _submit(client, pid, headers, 'graph-chain-0001')
    tid, rid = receipt['task_id'], receipt['execution_id']
    wait_state(store, rid, {'awaiting_approval'})

    _, nodes, edges = _graph(client)
    task, plan, check = nodes[f'task:{tid}'], nodes[f'plan:{tid}'], nodes[f'check:{tid}']
    assert edges[(f"req:{receipt['requirement_id']}", f'task:{tid}')] == 'creates'
    assert edges[(f'task:{tid}', f'plan:{tid}')] == 'plans'
    assert edges[(f'plan:{tid}', f'check:{tid}')] == 'verified_by'
    assert edges[(f'check:{tid}', f'target:{tid}')] == 'targets'
    # Waiting for approval hangs on the plan, where the work actually stopped.
    assert edges[(f'plan:{tid}', f'blocker:{tid}')] == 'blocked_by'
    assert plan['status'] == 'awaiting_approval' and plan['tone'] == 'attention'
    assert check['status'] == 'none' and check['label'] == '尚无检查记录'  # not inferred as passed
    facts = _facts(task)
    assert facts['负责人'].startswith('未记录') and facts['执行发起人'] == 'owner'
    assert facts['当前可处理'] == '发起人 owner 或管理员' and facts['基线版本'] != '未记录'
    assert _facts(nodes[f'repo:{pid}'])['负责人'].startswith('未记录')
    assert {'kind': 'task', 'id': tid, 'label': '任务现场'} in task['links']
    assert {'kind': 'repo', 'id': pid, 'label': '仓库详情'} in nodes[f'repo:{pid}']['links']
    assert not any(n['type'] == 'artifact' for n in nodes.values())

    _deliver(client, store, tid, rid, headers)
    _, nodes, edges = _graph(client)
    check = nodes[f'check:{tid}']
    assert check['status'] == 'passed' and _facts(check)['greeting'] == '通过'
    patch = next(n for n in nodes.values() if n['id'].startswith(f'artifact:{tid}:'))
    assert edges[(f'check:{tid}', patch['id'])] == 'produces'
    assert _facts(patch)['说明'] == '已生成 ≠ 已验收 ≠ 已上线'
    assert f'blocker:{tid}' not in nodes and f'target:{tid}' not in nodes


def test_projection_never_names_a_project_the_caller_cannot_read(app_env, tmp_path):
    client, store, svc, repo = app_env
    headers = login(client)
    secret = repo.parent / 'secret-crm'
    secret.mkdir()
    for args in (['init', '-q', '-b', 'main'], ['-c', 'user.name=t', '-c', 'user.email=t@e', 'commit', '-q',
                                                 '--allow-empty', '-m', 'base']):
        subprocess.run(['git', *args], cwd=secret, check=True, capture_output=True)
    mine = _register(client, repo, headers)['project_id']
    theirs = client.post('/api/v2/maintenance/repos', json={'source': str(secret), 'name': '销售机密仓库'},
                         headers=headers).json()['project_id']
    _submit(client, theirs, headers, 'graph-secret-0001')

    _member(client, mine, 'graph-member')
    body, nodes, _ = _graph(client)
    assert {p['id'] for p in body['projects']} == {mine}
    assert all(n['project_id'] == mine for n in nodes.values())
    assert '销售机密仓库' not in str(body) and theirs not in str(body)
    assert client.get('/api/v2/maintenance/graph', params={'project_id': theirs}).status_code in (403, 404)


def test_a_failed_repo_node_says_why_and_what_next(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    from factory.control import maintenance_subsystem as ms
    real = subprocess.run
    monkeypatch.setattr(ms.subprocess, 'run', lambda argv, *a, **k: subprocess.CompletedProcess(
        argv, 128, '', 'fatal: unable to access: Could not resolve host: example.invalid') if argv[:2] == ['git', 'clone'] else real(argv, *a, **k))
    res = client.post('/api/v2/maintenance/repos', json={'source': 'https://example.invalid/a/b.git', 'name': 'broken'},
                      headers=headers)
    pid = res.json()['project_id']
    import time
    for _ in range(100):
        if client.get(f'/api/v2/maintenance/repos/{pid}').json()['state'] == 'failed':
            break
        time.sleep(0.05)
    _, nodes, _ = _graph(client)
    facts = _facts(nodes[f'repo:{pid}'])
    assert nodes[f'repo:{pid}']['tone'] == 'blocked'
    assert facts['失败原因'] == '执行主机无法连接代码托管' and '重新接入' in facts['下一步']
