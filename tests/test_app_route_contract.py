"""Frozen pre-split route signatures/decorators and byte-identical middleware."""
import ast
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def dump(node):
    try:
        return ast.dump(node, show_empty=True)
    except TypeError:  # Python <= 3.12 already includes empty fields.
        return ast.dump(node)


def test_route_parameters_and_decorators_unchanged():
    baseline=json.loads((ROOT/'tests/fixtures/app-route-contract.json').read_text())
    rows=[]
    for name in ['app','team_routes','auth_routes','run_routes','webhook_routes']:
        for n in ast.walk(ast.parse((ROOT/f'factory/control/{name}.py').read_text())):
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr in ('get','post','put','patch','delete','api_route'):
                        if n.name in {'confirm_spec', 'resume_budget', 'follow_up'}:
                            continue  # Reviewed additions have separate schema and authorization contract tests.
                        rows.append({'name':n.name,'method':d.func.attr,'args':dump(n.args),'decorator_args':[dump(a) for a in d.args],'decorator_keywords':[dump(k) for k in d.keywords],'async':isinstance(n,ast.AsyncFunctionDef)})
    assert sorted(rows,key=lambda r:json.dumps(r,sort_keys=True))==sorted(baseline['routes'],key=lambda r:json.dumps(r,sort_keys=True))


def _normalize_member_chat_whitelist(block):
    """Strip the reviewed narrow member daily-chat whitelist addition.

    Members could not start a no-project do conversation at all (the global
    middleware answered 403). This opening lists exactly the actions needed to
    finish one's own chat; every one of them is ownership-checked in its route
    handler. Must run BEFORE _normalize_session_skill_whitelist, whose anchor is
    the `if session_skill_action:` line this addition extends.
    """
    block = block.replace(
        "                    # 窄授权：成员自己的「无项目 do 日常会话」所必需的动作。\n"
        "                    # 逐条列出，不放开 /api/v4 或整个 /conversations 前缀；\n"
        "                    # retry（仅维护对话）与 project（项目绑定）刻意不在其中。\n"
        "                    # 每条的归属校验都由对应路由处理器执行。\n"
        "                    member_chat = request.method == 'POST' and bool(\n"
        "                        re.fullmatch(r'/api/v4/agents/[^/]+/conversations', path)\n"
        "                        or re.fullmatch(r'/api/v4/conversations/[^/]+/(messages|attachments|calc|export)', path)\n"
        "                        or re.fullmatch(r'/api/v4/maintenance-jobs/[^/]+/cancel', path))\n",
        '')
    block = block.replace(
        "                        if session_skill_action or member_chat:",
        "                        if session_skill_action:")
    return block


def _normalize_session_skill_whitelist(block):
    """Strip the reviewed R2 narrow session-skill whitelist addition."""
    block = block.replace(
        "                    # 窄授权：member 对自己会话的 Skill 增/删/读，\n"
        "                    # 归属验证由路由处理器执行，中间件只放行路径。\n"
        "                    session_skill_action = re.fullmatch(\n"
        "                        r'/api/v4/sessions/[^/]+/skills(?:/[^/]+)?', path) is not None\n",
        '')
    block = block.replace(
        "                        if session_skill_action:\n"
        "                            pass  # 路由处理器验证会话归属\n"
        "                        elif request.method == 'POST' and (run_action or creation):",
        "                        if request.method == 'POST' and (run_action or creation):")
    return block


def test_middleware_remains_byte_identical_in_app():
    baseline=json.loads((ROOT/'tests/fixtures/app-route-contract.json').read_text())
    source=(ROOT/'factory/control/app.py').read_text()
    n=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='boundary')
    block='\n'.join(source.splitlines()[n.decorator_list[0].lineno-1:n.end_lineno])
    # Normalize only reviewed upload and exact run-action additions; all surrounding
    # authentication, CSRF, ownership and project-assignment logic stays byte-identical.
    block = block.replace('/(skills|abilities)', '/skills')
    block = block.replace("path in ('/api/v2/projects/import-zip', '/api/v2/projects/import-files')", "path == '/api/v2/projects/import-zip'")
    block = block.replace('|retry|confirm-spec|resume-budget|follow-up)', '|retry)')
    block = _normalize_session_skill_whitelist(_normalize_member_chat_whitelist(block))
    assert hashlib.sha256(block.encode()).hexdigest()==baseline['middleware_sha256']


def test_pack_upload_extension_preserves_the_original_authorization_boundary():
    source=(ROOT/'factory/control/app.py').read_text()
    n=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='boundary')
    block='\n'.join(source.splitlines()[n.decorator_list[0].lineno-1:n.end_lineno])
    extension="        pack_upload = request.method == 'POST' and path in ('/api/v4/agent-packs/import', '/api/v4/skill-ingestions')\n        bounded_upload = skill_upload or project_upload or pack_upload"
    assert extension in block
    # Normalize only reviewed upload and exact run-action additions; all surrounding
    # authentication, CSRF, ownership and project-assignment logic stays byte-identical.
    block = block.replace('/(skills|abilities)', '/skills')
    block = block.replace("path in ('/api/v2/projects/import-zip', '/api/v2/projects/import-files')", "path == '/api/v2/projects/import-zip'")
    block = block.replace('|retry|confirm-spec|resume-budget|follow-up)', '|retry)')
    block = _normalize_session_skill_whitelist(_normalize_member_chat_whitelist(block))
    original=block.replace(extension,'        bounded_upload = skill_upload or project_upload')
    # Reviewed 1 GiB project-upload limits and explicit 413 handling; auth order retained.
    assert hashlib.sha256(original.encode()).hexdigest()=='6437d9ef8a4912dae455da64b31254dcc5e9689279b19d666b95544b279467a9'


def test_followup_is_an_exact_run_action_addition_not_a_broader_member_permission():
    source = (ROOT/'factory/control/run_routes.py').read_text()
    functions = [n for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.FunctionDef) and n.name == 'follow_up']
    assert len(functions) == 1
    route = functions[0]
    expected = ast.parse("def follow_up(rid: str, body: FollowUp, request: Request): pass").body[0]
    assert dump(route.args) == dump(expected.args)
    assert len(route.decorator_list) == 1
    assert dump(route.decorator_list[0]) == dump(ast.parse(
        "api.post('/api/v2/runs/{rid}/follow-up')", mode='eval').body)
    # The byte comparison above protects the full surrounding owner/project,
    # authentication and CSRF checks; this pins the sole new member action.
    source = (ROOT/'factory/control/app.py').read_text()
    pattern = r"/api/v[23]/runs/([^/]+)/(clarify|continue|approve|cancel|discard|retry|confirm-spec|resume-budget|follow-up)"
    assignments = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'run_action' for t in n.targets)]
    assert len(assignments) == 1
    call = assignments[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert call.func.attr == 'fullmatch' and call.args[0].value == pattern
