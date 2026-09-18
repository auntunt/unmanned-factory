"""N7: 非 general 操作在 new_run 中写入 delivery_type_inferred。"""
import pytest
from tests.test_control_app import app_env, login, project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_run(client, headers, pid, operation, request_text):
    """POST /api/v2/runs and return (response_json, status_code)."""
    resp = client.post('/api/v2/runs', headers=headers, json={
        'operation': operation,
        'project_id': pid,
        'request': request_text,
    })
    return resp.json(), resp.status_code


# ---------------------------------------------------------------------------
# 1. 非 general 操作，文本唯一命中 → 写入正确 delivery_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('operation,request_text,expected_type', [
    ('bugfix', '修复网站的登录接口', 'service'),
    ('release', '发布命令行工具 v2.0', 'cli'),
    ('startup', '初始化一个桌面客户端项目', 'installer'),
    ('dependencies', '更新在线服务的依赖包', 'service'),
])
def test_non_general_unique_match_writes_delivery_type(app_env, operation, request_text, expected_type):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    data, status = _create_run(client, headers, pid, operation, request_text)
    assert status == 201
    run = store.get(data['id'])
    assert run.get('delivery_type_inferred') == expected_type


# ---------------------------------------------------------------------------
# 2. 歧义文本 → 不写（保持未声明）
# ---------------------------------------------------------------------------

def test_non_general_ambiguous_text_does_not_write(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    data, status = _create_run(client, headers, pid, 'bugfix', '帮我写个代码')
    assert status == 201
    run = store.get(data['id'])
    assert run.get('delivery_type_inferred') is None


# ---------------------------------------------------------------------------
# 3. general 操作路径行为不变
# ---------------------------------------------------------------------------

def test_general_operation_unchanged(app_env):
    """general 操作不在 new_run 中写 delivery_type_inferred（由需求分析流程处理）。"""
    client, store, svc, repo = app_env
    headers = login(client)
    pid = project(client, repo, headers)['id']
    data, status = _create_run(client, headers, pid, 'general', '做一个网站')
    assert status == 201
    run = store.get(data['id'])
    # general 路径的 delivery_type_inferred 由 requirement_analysis.confirm() 写入，
    # 不是 new_run()。此处只确认 new_run 没有越权写入。
    assert run.get('delivery_type_inferred') is None
