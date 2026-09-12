from tests.review_helpers import passing_review
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from factory.control.workspaces import create_workspace
from tests.test_workbench_app import app_env
from tests.test_control_app import login


def test_name_and_budget_create_workspace_without_requirements(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    body = {'name': '我的设计工作区', 'budget_usd': 12, 'idempotency_key': 'create-workspace-1'}
    assert client.post('/api/v2/projects/create-workspace', json=body).status_code == 403
    response = client.post('/api/v2/projects/create-workspace', json=body, headers=headers)
    assert response.status_code == 201, response.text
    p = response.json()
    root = Path(p['workspace'])
    assert root.parent == repo.parent and root.name.startswith('workspace-')
    assert p['name'] == body['name'] and p['budget_usd'] == 12
    assert p['managed_workspace'] and not p['auto_publish']
    assert subprocess.check_output(['git', 'rev-parse', '--verify', 'main'], cwd=root)
    assert not subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', 'HEAD'], cwd=root)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
    assert service.policies.get(p['id'])['mode'] == 'autonomous'
    repeated = client.post('/api/v2/projects/create-workspace', json=body, headers=headers)
    assert repeated.json()['id'] == p['id']
    changed = client.post('/api/v2/projects/create-workspace', json={**body, 'name': '不同内容'}, headers=headers)
    assert changed.status_code == 409
    assert len(store.projects()) == 1


def test_creation_rolls_back_its_own_directory_on_git_failure(app_env, monkeypatch):
    client, store, _, repo = app_env
    from factory.control import workspaces
    before = set(repo.parent.iterdir())
    def fail(path): raise subprocess.CalledProcessError(1, 'git')
    monkeypatch.setattr(workspaces, 'initialize_repository', fail)
    response = client.post('/api/v2/projects/create-workspace', json={'name': '空工作区', 'idempotency_key': 'retryable-creation'}, headers=login(client))
    assert response.status_code == 503
    assert set(repo.parent.iterdir()) == before and not store.projects()


def test_concurrent_retries_provision_one_workspace(app_env):
    _, store, _, repo = app_env
    def make(_):
        return create_workspace(store, repo.parent, name='同一个工作区', budget_usd=10.0, actor_id=1, idempotency_key='parallel-request')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(make, range(2)))
    assert results[0]['id'] == results[1]['id']
    assert len(list(repo.parent.glob('workspace-*'))) == 1


def test_first_spoken_requirement_runs_in_fresh_workspace(app_env, monkeypatch):
    import json
    from factory.control.providers import ProviderResult
    from tests.test_control_app import wait_state
    client, store, service, _ = app_env
    headers = login(client)
    p = client.post('/api/v2/projects/create-workspace', json={'name': '新项目', 'budget_usd': 10, 'idempotency_key': 'first-task-workspace'}, headers=headers).json()
    verified = []
    def runner(request, emit, cancel=None):
        root = Path(request.workspace)
        if request.read_only and 'TASK ACCEPTANCE:' in request.prompt:
            assert (root / 'hello.txt').read_text() == '你好'
            verified.append(True)
            return ProviderResult(passing_review(request, '已核对文件内容'), cost_usd=.01)
        if request.read_only:
            return ProviderResult(json.dumps({'title': '写问候文件', 'summary': '生成一份中文问候文件', 'questions': [], 'tasks': [{'id': 'hello', 'title': '创建问候文件', 'prompt': '新增 hello.txt，内容为你好', 'acceptance': ['hello.txt 的内容是你好'], 'paths': ['hello.txt'], 'checks': ['workspace-integrity'], 'depends_on': [], 'complexity': 'small', 'risk': 'low'}]}), cost_usd=.01)
        (root / 'hello.txt').write_text('你好')
        return ProviderResult('文件已创建', cost_usd=.01)
    monkeypatch.setattr(service.runner, 'run', runner)
    response = client.post('/api/v2/runs', json={'project_id': p['id'], 'request': '帮我写一个文件说你好'}, headers=headers)
    assert response.status_code == 201, response.text
    run = wait_state(store, response.json()['id'], {'ready_for_review', 'failed', 'needs_human', 'needs_clarification'})
    assert run['status'] == 'ready_for_review', run
    assert verified == [True] and run['artifacts']['verification']['verdict'] == 'pass'
    result = client.get(f"/api/v3/runs/{run['id']}/deliverables").json()
    assert result['saved'] and any(item['name'] == 'hello.txt' for item in result['items'])


def test_short_spoken_goal_is_accepted_for_planning(app_env, monkeypatch):
    client, _, service, _ = app_env
    headers = login(client)
    p = client.post('/api/v2/projects/create-workspace', json={'name': '口语入口', 'idempotency_key': 'short-goal-workspace'}, headers=headers).json()
    planning = []
    monkeypatch.setattr(service, 'start_plan', planning.append)
    response = client.post('/api/v2/runs', json={'project_id': p['id'], 'request': '画图'}, headers=headers)
    assert response.status_code == 201, response.text
    assert planning == [response.json()['id']]
    assert client.post('/api/v2/runs', json={'project_id': p['id'], 'request': '   '}, headers=headers).status_code == 422
