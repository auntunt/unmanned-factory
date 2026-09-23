"""Frozen pre-split route signatures/decorators and byte-identical middleware."""
import ast
import hashlib
import json
from pathlib import Path

from tests.test_control_app import app_env  # noqa: F401

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
                        if n.name in {'confirm_spec', 'resume_budget', 'follow_up', 'import_workspace'}:
                            continue  # Reviewed changes have separate schema and authorization contract tests.
                        rows.append({'name':n.name,'method':d.func.attr,'args':dump(n.args),'decorator_args':[dump(a) for a in d.args],'decorator_keywords':[dump(k) for k in d.keywords],'async':isinstance(n,ast.AsyncFunctionDef)})
    frozen = [r for r in baseline['routes'] if r['name'] != 'import_workspace']
    assert sorted(rows,key=lambda r:json.dumps(r,sort_keys=True))==sorted(frozen,key=lambda r:json.dumps(r,sort_keys=True))


def _normalize_maintenance_boundary(block):
    """Undo the four reviewed maintenance-subsystem additions (f2ee5b6, 7b6d684).

    Each one is pinned by a behaviour test, so the byte comparison keeps guarding
    everything around them:
    - machine intake bypasses the session only for POST on that exact path and
      authenticates its own bearer token: test_machine_intake_exemption_is_exact_and_token_checked;
    - member task actions need the execution's owner plus project assignment:
      test_maintenance_subsystem.py::test_member_cannot_act_on_someone_elses_task_and_is_not_offered_to;
    - member requirement submission is project-checked in the route:
      test_maintenance_subsystem.py::test_member_without_the_project_cannot_submit_or_act;
    - framing is opened only for /embed/ and only when configured:
      test_embed_framing_is_opt_in_and_never_reaches_the_api.
    Must run BEFORE _normalize_member_chat_whitelist, whose anchors predate these.
    """
    pairs = [
        ("        # The maintenance machine-intake route authenticates its own bearer token\n"
         "        # (an intake source, scoped to its projects); it has no browser session.\n"
         "        machine_intake = request.method == 'POST' and path == '/api/v2/maintenance/intake'\n"
         "        public_api = path in ('/api/auth/login', '/api/v2/github/webhook') or machine_intake\n",
         "        public_api = path in ('/api/auth/login', '/api/v2/github/webhook')\n"),
        ("if path != '/api/v2/github/webhook' and not machine_intake and request.headers.get('origin') != origin:",
         "if path != '/api/v2/github/webhook' and request.headers.get('origin') != origin:"),
        ("                    # 运维维护任务上的同一组「自己发起的运行」动作；授权规则与 run_action\n"
         "                    # 完全相同（执行的发起人 + 项目授权），只是先从任务找到它的执行。\n"
         "                    maintenance_action = re.fullmatch(\n"
         "                        r'/api/v2/maintenance/tasks/([^/]+)/(clarify|approve|follow-up|resume|cancel|feedback)', path)\n",
         ""),
        ("                        or re.fullmatch(r'/api/v4/maintenance-jobs/[^/]+/cancel', path)\n"
         "                        # 运维维护子系统的人工需求提交：项目授权由路由内的身份端口校验。\n"
         "                        or path == '/api/v2/maintenance/requirements')\n",
         "                        or re.fullmatch(r'/api/v4/maintenance-jobs/[^/]+/cancel', path))\n"),
        ("                        elif request.method == 'POST' and (run_action or creation or maintenance_action):\n"
         "                            if maintenance_action:\n"
         "                                from factory.control.maintenance_subsystem import task_execution_owner\n"
         "                                owner_id, project_id = task_execution_owner(store, maintenance_action[1])\n"
         "                                if owner_id != user['id']:\n"
         "                                    raise AuthError('成员只能操作自己发起的维护任务', 403)\n"
         "                            elif run_action:\n",
         "                        elif request.method == 'POST' and (run_action or creation):\n"
         "                            if run_action:\n"),
        ("        if embed_origins and path.startswith('/embed/'):\n"
         "            # Opt-in host embedding of the maintenance subsystem pages only; every\n"
         "            # other page and the whole API stay unframeable.\n"
         "            response.headers['Content-Security-Policy'] = 'frame-ancestors ' + ' '.join(embed_origins)\n"
         "        else:\n"
         "            response.headers['X-Frame-Options'] = 'DENY'\n",
         "        response.headers['X-Frame-Options'] = 'DENY'\n"),
    ]
    for new, old in pairs:
        # A reviewed hunk that no longer matches is a middleware change nobody
        # reviewed here: fail loudly instead of hashing around it.
        assert block.count(new) == 1, new
        block = block.replace(new, old)
    return block


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


def test_zip_import_form_exposes_no_dollar_field():
    """The removal of budget_usd from import-zip is the only signature change."""
    source = (ROOT/'factory/control/app.py').read_text()
    routes = [n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == 'import_workspace']
    assert len(routes) == 1
    args = [a.arg for a in routes[0].args.args]
    assert args == ['request', 'file', 'name', 'idempotency_key', 'agent_id']
    assert len(routes[0].decorator_list) == 1
    assert dump(routes[0].decorator_list[0]) == dump(ast.parse(
        "app.post('/api/v2/projects/import-zip', status_code=201)", mode='eval').body)


def _normalize_resume_budget_removal(block):
    """Undo the reviewed REMOVAL of resume-budget from the member allowlist.

    Renewing a dollar ceiling became admin-only; the route handler additionally
    checks the role. This maps the narrowed pattern back to the frozen text so the
    byte comparison still protects everything around it.
    """
    return block.replace(
        "                    # resume-budget is deliberately absent: renewing a ceiling is\n"
        "                    # an admin decision, not something a member may do on a run\n"
        "                    # they happen to own.\n"
        "                    run_action = re.fullmatch(r'/api/v[23]/runs/([^/]+)/(clarify|continue|approve|cancel|discard|retry|confirm-spec|follow-up)', path)",
        "                    run_action = re.fullmatch(r'/api/v[23]/runs/([^/]+)/(clarify|continue|approve|cancel|discard|retry|confirm-spec|resume-budget|follow-up)', path)")


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
    block = _normalize_resume_budget_removal(block)
    block = block.replace('|retry|confirm-spec|resume-budget|follow-up)', '|retry)')
    block = _normalize_session_skill_whitelist(_normalize_member_chat_whitelist(_normalize_maintenance_boundary(block)))
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
    block = _normalize_resume_budget_removal(block)
    block = block.replace('|retry|confirm-spec|resume-budget|follow-up)', '|retry)')
    block = _normalize_session_skill_whitelist(_normalize_member_chat_whitelist(_normalize_maintenance_boundary(block)))
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
    # resume-budget was withdrawn from this allowlist: raising a ceiling is admin-only.
    pattern = r"/api/v[23]/runs/([^/]+)/(clarify|continue|approve|cancel|discard|retry|confirm-spec|follow-up)"
    assignments = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'run_action' for t in n.targets)]
    assert len(assignments) == 1
    call = assignments[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert call.func.attr == 'fullmatch' and call.args[0].value == pattern



def test_machine_intake_exemption_is_exact_and_token_checked(app_env):
    """The only session-less write: POST on the exact intake path, with its own token."""
    client, store, svc, repo = app_env
    body = {'project_id': 'x', 'content': 'hello', 'external_id': 'ext-1'}  # structurally valid
    no_token = client.post('/api/v2/maintenance/intake', json=body)
    assert no_token.status_code == 401
    assert client.post('/api/v2/maintenance/intake', json=body, headers={'Authorization': 'Bearer wbm_forged'}).status_code == 401
    # Neighbouring paths and other methods keep session + origin rules.
    assert client.get('/api/v2/maintenance/intake').status_code == 401
    assert client.post('/api/v2/maintenance/intake/x', json=body).status_code == 403  # origin check still applies
    assert client.post('/api/v2/maintenance/intake-sources', json={}, headers={'Origin': 'http://testserver'}).status_code == 401


def test_embed_framing_is_opt_in_and_never_reaches_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from factory.control.app import create_app
    for origins, embed_header in ((None, None), ('https://host.example', 'frame-ancestors https://host.example')):
        if origins:
            monkeypatch.setenv('FACTORY_EMBED_ORIGINS', origins)
        else:
            monkeypatch.delenv('FACTORY_EMBED_ORIGINS', raising=False)
        app = create_app(data_dir=tmp_path / f'd{bool(origins)}', workspace_root=tmp_path,
                         public_origin='http://testserver', webhook_secret='s', static_dir=tmp_path / 'nostatic')
        app.state.auth.create_user('frame-admin', 'a-long-test-password')
        with TestClient(app) as client:
            # Signed in, so the API answer goes through the normal response path
            # (early 401s are answered before any security header is added).
            assert client.post('/api/auth/login', json={'username': 'frame-admin', 'password': 'a-long-test-password'},
                               headers={'Origin': 'http://testserver'}).status_code == 200
            embed = client.get('/embed/maintenance/')
            assert embed.headers.get('content-security-policy') == embed_header
            assert (embed.headers.get('x-frame-options') == 'DENY') == (embed_header is None)
            for path in ('/api/auth/me', '/', '/maintenance'):
                r = client.get(path)
                assert r.headers.get('x-frame-options') == 'DENY' and 'content-security-policy' not in r.headers, path
