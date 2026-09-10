"""Trusted-team authenticated web gateway. Run one process; workers are bounded."""
from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from factory.control.auth import AuthError, AuthStore
from factory.control.github import GitHubDelivery, REPOSITORY, verify_signature
from factory.control.service import Service
from factory.control.store import Conflict, Store, now, scrub

class GitHubPublishRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['existing', 'create', 'bound']
    repository: str | None = Field(default=None, max_length=240)
    name: str | None = Field(default=None, max_length=100)
    private: Literal[True] = True
    expected_project_revision: int = Field(ge=1)


COOKIE = 'factory_session'


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(Body):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=4096)
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)


class Project(Body):
    name: str = Field(min_length=1, max_length=120)
    repository: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')
    workspace: str = Field(min_length=1, max_length=4096)
    base_branch: str = Field(default='main', min_length=1, max_length=200)
    checks: dict[str, list[str]] = Field(default_factory=dict)
    auto_issues: bool = False
    auto_publish: bool = False
    budget_usd: float = Field(default=10.0, gt=0, le=1000, allow_inf_nan=False)


class ProjectUpdate(Body):
    revision: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=120)
    base_branch: str = Field(min_length=1, max_length=200)
    checks: dict[str, list[str]] = Field(default_factory=dict)
    auto_issues: bool = False
    auto_publish: bool = False
    budget_usd: float = Field(default=10.0, gt=0, le=1000, allow_inf_nan=False)


class ConnectProject(Body):
    agent_id: str | None = None
    candidate_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    name: str | None = Field(default=None, min_length=1, max_length=120)
    checks: dict[str, list[str]] = Field(default_factory=dict)
    auto_issues: bool = False
    auto_publish: bool = False
    budget_usd: float = Field(default=10.0, gt=0, le=1000, allow_inf_nan=False)


class NewWorkspace(Body):
    agent_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    budget_usd: float = Field(default=10.0, gt=0, le=1000, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


class NewRun(Body):
    project_id: str
    request: str = Field(min_length=1, max_length=50_000)


class Continuation(Body):
    answer: str = Field(default="", max_length=20000)
    revision: int = Field(ge=1)
    resume_count: int = Field(default=0, ge=0)


class Clarification(Body):
    answer: str = Field(min_length=1, max_length=50_000)


class Approval(Body):
    revision: int = Field(ge=1)


def create_app(*, data_dir=None, workspace_root=None, public_origin=None, service=None,
               webhook_secret=None, static_dir=None):
    data = Path(data_dir or os.getenv('FACTORY_CONTROL_DATA', '~/.factory/control')).expanduser().resolve()
    allowed_root = Path(workspace_root or os.getenv('FACTORY_WORKSPACE_ROOT', '~/projects')).expanduser().resolve()
    origin = (public_origin or os.getenv('FACTORY_PUBLIC_ORIGIN', 'http://127.0.0.1:8788')).rstrip('/')
    parsed_origin = urlparse(origin)
    if parsed_origin.scheme not in ('http', 'https') or not parsed_origin.hostname or parsed_origin.path:
        raise ValueError('FACTORY_PUBLIC_ORIGIN 必须是完整源地址，例如 https://factory.example.com')
    if parsed_origin.scheme == 'http' and parsed_origin.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
        raise ValueError('公网登录必须使用 HTTPS')
    auth = AuthStore(data / 'users.db')
    store = service.store if service else Store(data / 'control.db')
    token = os.getenv('FACTORY_GITHUB_TOKEN', '')
    svc = service or Service(store, publisher=GitHubDelivery(token) if token else None,
                            timeout_s=int(os.getenv('FACTORY_TASK_TIMEOUT', '14400')))
    from factory.control.governance import Governance
    governance = Governance(auth, store)
    svc.governance = governance
    # Runtime settings are persisted in the control store. Root may initialize
    # this on Service; keeping the fallback here preserves compatibility with
    # injected test services and older callers.
    from factory.control.runtime import RuntimeSettings
    if not hasattr(svc, 'runtime_settings'):
        svc.runtime_settings = RuntimeSettings(store)
    secret = webhook_secret if webhook_secret is not None else os.getenv('FACTORY_WEBHOOK_SECRET', '')
    static = Path(static_dir or os.getenv('FACTORY_STATIC_DIR') or Path(__file__).resolve().parents[2] / 'frontend' / 'dist').expanduser().resolve()

    @asynccontextmanager
    async def lifespan(app):
        if hasattr(svc, 'recover'):
            svc.recover()
        else:
            store.recover()
        try:
            yield
        finally:
            svc.close()

    app = FastAPI(title='webuddy', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.auth, app.state.service, app.state.store = auth, svc, store
    app.state.governance = governance
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed_origin.hostname, '127.0.0.1', 'localhost'])
    from factory.control.runtime_routes import router as runtime_router
    app.include_router(runtime_router(store, svc, allowed_root, static))
    from factory.control.autonomy_routes import router as autonomy_router
    from factory.control.capability_routes import router as capability_router
    app.include_router(autonomy_router(store, svc))
    app.include_router(capability_router(store, svc))
    from factory.control.team_routes import router as team_router
    app.include_router(team_router(auth, governance))
    from factory.control.deliverables import router as deliverables_router
    app.include_router(deliverables_router(store, svc))
    from factory.control.agent_routes import router as agent_router
    app.include_router(agent_router(store, svc))

    @app.exception_handler(AuthError)
    async def auth_error(req, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)

    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler

    @app.exception_handler(RequestValidationError)
    async def invalid_request(req, exc):
        if req.url.path == '/api/v2/runtime/probe' and any(e['type'] == 'missing' for e in exc.errors()):
            return JSONResponse({'detail': '模型检查请求缺少当前配置版本或模型档位。请刷新页面后重试；旧版页面无法发起此检查。'}, status_code=422)
        return await request_validation_exception_handler(req, exc)

    @app.exception_handler(Conflict)
    async def conflict(req, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(req, exc):
        return JSONResponse({'detail': '记录不存在'}, status_code=404)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        path = request.url.path
        public_api = path in ('/api/auth/login', '/api/v2/github/webhook')
        is_api = path.startswith('/api/')
        skill_upload = request.method == 'POST' and re.fullmatch(r'/api/v4/agents/[^/]+/skills', path) is not None
        project_upload = request.method == 'POST' and path == '/api/v2/projects/import-zip'
        bounded_upload = skill_upload or project_upload
        # Authenticate and check CSRF before opening the bounded upload stream;
        # an unauthenticated upload must not be buffered into memory first.
        if bounded_upload:
            if request.headers.get('origin') != origin:
                return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
            early_user = auth.authenticate(request.cookies.get(COOKIE, ''))
            if not early_user:
                return JSONResponse({'detail': '请先登录'}, status_code=401)
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), early_user['csrf_token']):
                return JSONResponse({'detail': '会话验证失败，请重新登录'}, status_code=403)
            if early_user['role'] != 'admin':
                return JSONResponse({'detail': '此操作需要管理员权限'}, status_code=403)
        if bounded_upload:
            # Let UploadFile spool multipart parts; do not buffer the archive
            # again in the control-plane middleware before parsing it.
            upstream_receive = request._receive
            upload_size = 0
            async def limited_receive():
                nonlocal upload_size
                message = await upstream_receive()
                upload_size += len(message.get('body', b''))
                if upload_size > 21 * 1024 * 1024:
                    raise HTTPException(413, '请求体过大')
                return message
            request._receive = limited_receive
        if not bounded_upload and request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 1_048_576:
                    return JSONResponse({'detail': '请求体过大'}, status_code=413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
            if path != '/api/v2/github/webhook' and request.headers.get('origin') != origin:
                return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
        if is_api and not public_api:
            user = auth.authenticate(request.cookies.get(COOKIE, ''))
            if not user:
                return JSONResponse({'detail': '请先登录'}, status_code=401)
            request.state.user = user
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), user['csrf_token']):
                    return JSONResponse({'detail': '会话验证失败，请重新登录'}, status_code=403)
                if user['role'] != 'admin':
                    # This is a shared workspace. Members may dispatch only
                    # assigned projects and act on their own runs. New write
                    # endpoints are admin-only unless explicitly listed here.
                    own_account = path in ('/api/auth/logout', '/api/auth/password') and request.method == 'POST'
                    run_action = re.fullmatch(r'/api/v[23]/runs/([^/]+)/(clarify|continue|approve|cancel|discard|retry)', path)
                    creation = path == '/api/v2/runs' or re.fullmatch(r'/api/v3/capabilities/[^/]+/invoke', path)
                    try:
                        if request.method == 'POST' and (run_action or creation):
                            if run_action:
                                target = store.get(run_action[1])
                                if target.get('source', {}).get('actor_id') != user['id']:
                                    raise AuthError('成员只能操作自己发起的运行', 403)
                                project_id = target['project_id']
                            else:
                                body = await request.json()
                                project_id = body.get('project_id') if isinstance(body, dict) else None
                                if not isinstance(project_id, str):
                                    raise AuthError('请选择项目', 422)
                            governance.require_project(user['id'], project_id)
                        elif not own_account:
                            raise AuthError('此操作需要管理员权限', 403)
                    except AuthError as exc:
                        return JSONResponse({'detail': str(exc)}, status_code=exc.status)
                    except KeyError:
                        return JSONResponse({'detail': '记录不存在'}, status_code=404)
                    except (ValueError, TypeError):
                        return JSONResponse({'detail': '请求格式无效'}, status_code=422)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if is_api or response.headers.get('content-type', '').startswith('text/html') or response.status_code == 404:
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/api/auth/login')
    def login(body: Login):
        token, csrf, user = auth.login(body.username, body.password)
        response = JSONResponse({'user': user, 'csrf_token': csrf})
        response.set_cookie(COOKIE, token, httponly=True, secure=parsed_origin.scheme == 'https',
                            samesite='strict', max_age=43200, path='/')
        return response

    @app.get('/api/auth/me')
    def me(request: Request):
        user = request.state.user
        return {'user': {key: user[key] for key in ('id', 'username', 'role', 'active')}, 'csrf_token': user['csrf_token']}

    @app.post('/api/auth/logout')
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE, ''))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path='/')
        return response

    @app.get('/api/v2/projects')
    def projects():
        return {'projects': store.projects()}

    @app.post('/api/v2/projects', status_code=201)
    def create_project(body: Project):
        root = Path(body.workspace).expanduser().resolve()
        if not root.is_relative_to(allowed_root) or root == allowed_root:
            raise HTTPException(400, '仓库必须位于 FACTORY_WORKSPACE_ROOT 的独立子目录')
        if not root.is_dir():
            raise HTTPException(400, '服务器上的仓库目录不存在')
        validate_check_definitions(body.checks)
        validate_project_git(root, body.base_branch)
        return store.add_project({**body.model_dump(), 'workspace': str(root)})

    @app.post('/api/v2/projects/create-workspace', status_code=201)
    def new_workspace(body: NewWorkspace, request: Request):
        from factory.control.workspaces import create_workspace, WorkspaceError
        try:
            from factory.control.project_assistants import ProjectAssistants
            helpers = ProjectAssistants(store)
            if body.agent_id:
                try: helpers.agents.get(body.agent_id)
                except KeyError: raise HTTPException(404, '所选职能体不存在') from None
            project = create_workspace(store, allowed_root, name=body.name, budget_usd=body.budget_usd,
                actor_id=request.state.user['id'], idempotency_key=body.idempotency_key)
            if body.agent_id and helpers.binding(project['id'])['revision'] == 0:
                helpers.bind(project['id'], body.agent_id, 0, request.state.user['id'])
            return project
        except WorkspaceError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post('/api/v2/projects/import-zip', status_code=201)
    def import_workspace(request: Request, file: UploadFile = File(...),
                         name: str = Form(..., min_length=1, max_length=120),
                         idempotency_key: str = Form(..., min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$'),
                         budget_usd: float = Form(10.0, gt=0, le=1000), agent_id: str | None = Form(None)):
        from factory.control.project_import import import_project, ImportError
        from factory.control.workspaces import WorkspaceError
        from factory.control.project_assistants import ProjectAssistants
        import math
        if not name.strip() or not math.isfinite(budget_usd):
            raise HTTPException(422, '请填写有效的项目名称和预算')
        helpers = ProjectAssistants(store)
        if agent_id:
            try: helpers.agents.get(agent_id)
            except KeyError: raise HTTPException(404, '所选职能体不存在') from None
        try:
            result = import_project(store, allowed_root, file.file, filename=file.filename,
                name=name.strip(), budget_usd=budget_usd, actor_id=request.state.user['id'],
                idempotency_key=idempotency_key, agent_id=agent_id)
            if agent_id and helpers.binding(result['project']['id'])['revision'] == 0:
                helpers.bind(result['project']['id'], agent_id, 0, request.state.user['id'])
            return result
        except ImportError as exc:
            raise HTTPException(400, str(exc)) from None
        except WorkspaceError as exc:
            raise HTTPException(503, str(exc)) from None
        finally:
            file.file.close()

    @app.get('/api/v2/project-candidates')
    def project_candidates(request: Request):
        if request.state.user['role'] != 'admin':
            raise HTTPException(403, '此操作需要管理员权限')
        from factory.control.project_discovery import discover
        candidates = discover(allowed_root, store.projects())
        return {'candidates': [{k: v for k, v in c.items() if k != 'workspace'} for c in candidates],
                'root_available': allowed_root.is_dir()}

    @app.post('/api/v2/projects/connect', status_code=201)
    def connect_project(body: ConnectProject, request: Request):
        from factory.control.project_assistants import ProjectAssistants
        helpers = ProjectAssistants(store)
        if body.agent_id:
            try: helpers.agents.get(body.agent_id)
            except KeyError: raise HTTPException(404, '所选职能体不存在') from None
        from factory.control.project_discovery import discover
        with svc.lock:
            candidate = next((c for c in discover(allowed_root, store.projects()) if c['id'] == body.candidate_id), None)
            if not candidate:
                raise HTTPException(409, '工程已发生变化或暂不可用，请刷新工程列表后重新选择')
            if candidate['registered']:
                raise Conflict('这个工程已经登记，请在项目列表中打开')
            if candidate['repository'].startswith('local/') and (body.auto_publish or body.auto_issues):
                raise HTTPException(400, '这个工程尚未连接 GitHub；可以先登记并下载成果，连接后再启用 GitHub 自动化')
            project = create_project(Project(name=body.name or candidate['name'][:120],
                repository=candidate['repository'], workspace=candidate['workspace'],
                base_branch=candidate['base_branch'], checks=body.checks,
                auto_issues=body.auto_issues, auto_publish=body.auto_publish, budget_usd=body.budget_usd))
            if body.agent_id:
                helpers.bind(project['id'], body.agent_id, 0, request.state.user['id'])
            return project

    def validate_check_definitions(checks):
        if len(checks) > 20:
            raise HTTPException(400, '最多配置 20 条检查')
        for name, argv in checks.items():
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', name) or not argv or len(argv) > 100
                    or any(not isinstance(a, str) or not a or len(a) > 4096 or '\x00' in a for a in argv)):
                raise HTTPException(400, '检查必须为名称到非空命令参数数组的映射')

    def validate_project_git(root, base_branch):
        for args in (['check-ref-format', '--branch', base_branch], ['rev-parse', '--show-toplevel'],
                     ['rev-parse', '--verify', f'refs/heads/{base_branch}']):
            try:
                result = subprocess.run(['git', *args], cwd=root, capture_output=True,
                                        text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                raise HTTPException(400, '无法验证项目 Git 仓库或基线分支') from None
            if result.returncode or (args[0] == 'rev-parse' and args[1] == '--show-toplevel'
                                     and Path(result.stdout.strip()).resolve() != root):
                raise HTTPException(400, '必须提供 Git 仓库根目录和存在的本地基线分支')

    @app.put('/api/v2/projects/{pid}')
    def update_project(pid: str, body: ProjectUpdate, request: Request):
        project = store.project(pid)
        root = Path(project['workspace']).expanduser().resolve()
        if not root.is_relative_to(allowed_root) or root == allowed_root:
            raise HTTPException(400, '仓库必须位于 FACTORY_WORKSPACE_ROOT 的独立子目录')
        validate_check_definitions(body.checks)
        validate_project_git(root, body.base_branch)
        try:
            return store.update_project(pid, body.model_dump(exclude={'revision'}), body.revision,
                                        request.state.user['username'])
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/v2/projects/{pid}/readiness')
    def project_readiness(pid: str):
        project = store.project(pid)
        root = Path(project['workspace']).expanduser().resolve()
        checks = []

        def add_check(identifier, label, status, message):
            checks.append({'id': identifier, 'label': label, 'status': status, 'message': message})

        if not root.is_relative_to(allowed_root) or root == allowed_root:
            add_check('workspace', '工作区', 'blocked', '工作区不在 FACTORY_WORKSPACE_ROOT 内')
            add_check('repository', 'Git 仓库', 'blocked', '无法检查 Git 仓库')
            add_check('base_branch', '基线分支', 'blocked', '工作区不可用')
            add_check('head_matches_base', 'HEAD 与基线', 'blocked', '工作区不可用')
            add_check('clean_head', '工作区洁净', 'blocked', '工作区不可用')
        elif not root.is_dir():
            add_check('workspace', '工作区', 'blocked', '工作区目录不存在')
            add_check('repository', 'Git 仓库', 'blocked', '无法检查 Git 仓库')
            add_check('base_branch', '基线分支', 'blocked', '工作区不可用')
            add_check('head_matches_base', 'HEAD 与基线', 'blocked', '工作区不可用')
            add_check('clean_head', '工作区洁净', 'blocked', '工作区不可用')
        else:
            def git(args):
                try:
                    return subprocess.run(['git', *args], cwd=root, capture_output=True,
                                          text=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    return None

            add_check('workspace', '工作区', 'ok', '工作区目录存在')
            repo = git(['rev-parse', '--show-toplevel'])
            if repo is None or repo.returncode != 0 or Path(repo.stdout.strip()).resolve() != root:
                add_check('repository', 'Git 仓库', 'blocked', '路径不是可用的 Git 仓库根目录')
            else:
                add_check('repository', 'Git 仓库', 'ok', 'Git 仓库根目录有效')
            branch = git(['rev-parse', '--verify', f"refs/heads/{project['base_branch']}"])
            if branch is None or branch.returncode != 0:
                add_check('base_branch', '基线分支', 'blocked', '本地基线分支不存在')
            else:
                add_check('base_branch', '基线分支', 'ok', '本地基线分支存在')
            head = git(['rev-parse', 'HEAD'])
            if (branch is None or branch.returncode != 0 or head is None or head.returncode != 0):
                add_check('head_matches_base', 'HEAD 与基线', 'blocked', '无法比较当前 HEAD 与基线')
            elif head.stdout.strip() != branch.stdout.strip():
                add_check('head_matches_base', 'HEAD 与基线', 'blocked', '当前 HEAD 不等于配置的本地基线')
            else:
                add_check('head_matches_base', 'HEAD 与基线', 'ok', '当前 HEAD 等于配置的本地基线')
            clean = git(['status', '--porcelain=v1', '--untracked-files=all'])
            if clean is None or clean.returncode != 0:
                add_check('clean_head', '工作区洁净', 'blocked', '无法检查工作区状态')
            elif clean.stdout:
                add_check('clean_head', '工作区洁净', 'blocked', '工作区有未提交或未跟踪改动')
            else:
                add_check('clean_head', '工作区洁净', 'ok', '工作区干净')

        validate_error = None
        try:
            validate_check_definitions(project.get('checks') or {})
        except HTTPException as exc:
            validate_error = str(exc.detail)
        if validate_error:
            add_check('checks', '检查定义', 'blocked', validate_error)
        elif not project.get('checks'):
            add_check('checks', '检查定义', 'blocked', '尚未配置可信检查')
        else:
            for name, argv in project['checks'].items():
                executable = argv[0] if argv else ''
                if Path(executable).is_absolute():
                    path = Path(executable)
                    found = path.is_file() and os.access(path, os.X_OK)
                elif '/' in executable or '\\' in executable:
                    # Checks execute with cwd=root; resolve relative paths the
                    # same way without shell-style ~ expansion.
                    path = (root / executable).resolve()
                    found = path.is_file() and os.access(path, os.X_OK)
                else:
                    found = bool(shutil.which(executable))
                add_check(f'check:{name}', f'检查 {name}', 'ok' if found else 'blocked',
                          '检查可执行文件存在' if found else f'找不到检查可执行文件：{executable}')
        return {'project_id': pid, 'ready': all(item['status'] == 'ok' for item in checks),
                'checks': checks, 'checked_at': now()}

    @app.get('/api/v2/providers')
    def providers():
        return {'providers': svc.runner.available(), 'profiles': svc.runtime_settings.get()['profiles']}

    @app.get('/api/v2/runs')
    def runs(project_id: str | None = None):
        if project_id is None:
            return {'runs': store.runs()}
        store.project(project_id)
        return {'runs': [run for run in store.all_runs() if run.get('project_id') == project_id]}

    @app.post('/api/v2/runs', status_code=201)
    def new_run(body: NewRun, request: Request):
        store.project(body.project_id)
        run, _ = store.create_run(body.project_id, body.request,
                                 source={'type': 'web', 'actor': request.state.user['username'],
                                         'actor_id': request.state.user['id']})
        try:
            svc.start_plan(run['id'])
        except Exception as exc:
            svc._fail(run['id'], exc)
            raise
        return run

    @app.get('/api/v2/runs/{rid}')
    def get_run(rid: str):
        from factory.control.engineering_overview import current_evidence
        run = store.get(rid)
        return {**run, 'progress': current_evidence(run)}

    @app.post('/api/v2/runs/{rid}/clarify')
    def clarify(rid: str, body: Clarification, request: Request):
        return svc.clarify(rid, body.answer, request.state.user['username'])

    @app.post('/api/v2/runs/{rid}/continue')
    def continue_run(rid: str, body: Continuation, request: Request):
        return svc.continue_run(rid, body.answer, body.revision, body.resume_count, request.state.user['username'])

    @app.post('/api/v2/runs/{rid}/approve')
    def approve(rid: str, body: Approval, request: Request):
        return svc.approve(rid, body.revision, request.state.user['username'])

    @app.post('/api/v2/runs/{rid}/cancel')
    def cancel(rid: str, request: Request):
        return svc.cancel(rid, request.state.user['username'])

    @app.post('/api/v2/runs/{rid}/discard')
    def discard(rid: str, request: Request):
        return svc.discard(rid, request.state.user['username'])

    @app.get('/api/v3/runs/{rid}/github-options')
    def github_options(rid: str, request: Request, page: int = 1):
        if request.state.user['role'] != 'admin':
            raise HTTPException(403, '此操作需要管理员权限')
        if not 1 <= page <= 100:
            raise HTTPException(422, '页码无效')
        try:
            return svc.github_options(rid, page=page)
        except (Conflict, KeyError):
            raise
        except Exception:
            raise HTTPException(502, '无法读取 GitHub 仓库，请检查凭据权限后重试。') from None

    @app.post('/api/v3/runs/{rid}/github-publish')
    def github_publish(rid: str, body: GitHubPublishRequest, request: Request):
        try:
            return svc.publish_github(rid, **body.model_dump(), actor=request.state.user['username'])
        except (Conflict, KeyError):
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception:
            message = (store.get(rid).get('artifacts') or {}).get('publish_error')
            raise HTTPException(502, message or 'GitHub 发布未完成；本地成果仍可查看和下载。请刷新仓库列表后重试。') from None

    @app.post('/api/v2/runs/{rid}/publish')
    def publish(rid: str):
        try:
            return svc.publish(rid)
        except (Conflict, KeyError):
            raise
        except Exception:
            message = (store.get(rid).get('artifacts') or {}).get('publish_error')
            raise HTTPException(502, message or 'GitHub 发布未完成；本地成果仍可查看和下载。') from None

    @app.get('/api/v2/runs/{rid}/events')
    def events(rid: str, after: int = 0):
        store.get(rid)
        rows = store.events(rid, max(0, after))
        return {'events': rows, 'cursor': rows[-1]['id'] if rows else max(0, after)}

    @app.get('/api/v2/runs/{rid}/conversation')
    def conversation(rid: str):
        store.get(rid)
        return {'messages': store.conversation(rid)}

    @app.get('/api/v2/runs/{rid}/export')
    def export(rid: str):
        run = store.get(rid)
        parts = [f"# 工程记录 {rid}", f"计划版本：{run['revision']} · 状态：{run['status']}"]
        for message in store.conversation(rid):
            parts.append(f"## {'用户' if message['role'] == 'user' else '系统'} · {message['at']}\n\n{message['content']}\n\n事件：{message['event_ids']}")
        parts.append('## 交付证据\n\n```json\n' + json.dumps(run['artifacts'], ensure_ascii=False, indent=2) + '\n```')
        if run.get('runtime_configuration'):
            parts.append('## 本次冻结的执行配置\n\n```json\n' +
                         json.dumps(run['runtime_configuration'], ensure_ascii=False, indent=2) + '\n```')
        if run.get('context'):
            parts.append('## 本次冻结的项目上下文\n\n```json\n' + json.dumps(run['context'], ensure_ascii=False, indent=2) + '\n```')
        return PlainTextResponse('\n\n'.join(parts), media_type='text/markdown',
            headers={'Content-Disposition': f'attachment; filename="factory-{rid}.md"'})

    @app.post('/api/v2/github/webhook')
    async def webhook(request: Request):
        raw = await request.body()
        if not verify_signature(raw, request.headers.get('x-hub-signature-256', ''), secret):
            raise HTTPException(401, 'Webhook 签名无效')
        event = request.headers.get('x-github-event')
        if event not in ('issues', 'pull_request'):
            return {'ignored': True}
        delivery = request.headers.get('x-github-delivery', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', delivery):
            raise HTTPException(400, '缺少有效 delivery id')
        if event == 'pull_request':
            try:
                payload = json.loads(raw)
                if payload.get('action') != 'closed':
                    return {'ignored': True}
                pull = payload['pull_request']
                if pull.get('merged') is not True:
                    return {'ignored': True, 'reason': 'not_merged'}
                repo = payload['repository']['full_name']
                number = payload['number']
                if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) or type(number) is not int or number < 1:
                    raise ValueError('invalid pull request identity')
            except (ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(400, '无效 GitHub Pull Request 事件') from None
            project = next((p for p in store.projects() if p['repository'].casefold() == repo.casefold()), None)
            if not project:
                return {'ignored': True, 'reason': 'repository_not_registered'}
            matching = store.published_runs_for_pr(project['id'], number, repo)
            if not matching:
                return {'ignored': True, 'reason': 'run_not_found'}
            # The signed event is only a trigger. Fetch GitHub's current PR state
            # independently; never promote webhook-provided SHAs or summaries.
            from starlette.concurrency import run_in_threadpool
            try:
                results = [await run_in_threadpool(svc.sync_merge, run['id']) for run in matching]
            except (Conflict, KeyError):
                raise
            except Exception:
                raise HTTPException(502, 'GitHub 合并状态核对失败；可重发事件或在控制台重试') from None
            return {'results': results}
        try:
            payload = json.loads(raw)
            if payload.get('action') not in ('opened', 'edited', 'labeled', 'reopened'):
                return {'ignored': True}
            repo = payload['repository']['full_name']
            issue = payload['issue']
            number = int(issue['number'])
            text = str(issue.get('title', '')) + '\n\n' + str(issue.get('body') or '')
            labels = {label['name'] for label in issue.get('labels', [])}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, '无效 GitHub Issue 事件') from None
        if 'pull_request' in issue or issue.get('state', 'open') != 'open':
            return {'ignored': True}
        project = next((p for p in store.projects() if p['repository'].casefold() == repo.casefold()), None)
        if not project:
            return {'ignored': True, 'reason': 'repository_not_registered'}
        # Deduplicate semantic issue revision as well as GitHub delivery id. Label events
        # with no content change can enable a single fresh, explicitly opted-in analysis.
        import hashlib
        semantic_id = hashlib.sha256(json.dumps([repo, number, issue.get('updated_at'), text,
                                                 'factory-ready' in labels]).encode()).hexdigest()
        run, created = store.create_run(project['id'], text[:50_000],
            source={'type': 'github', 'issue_number': number,
                    'url': f'https://github.com/{repo}/issues/{number}',
                    'trusted_label': 'factory-ready' in labels, 'delivery_id': delivery},
            delivery_id=delivery, semantic_id=semantic_id)
        if created:
            previous = run['source'].get('previous_run_id')
            if previous:
                try:
                    svc.cancel(previous, actor='issue-revision')
                except Conflict:
                    pass  # A verified/publishing result requires explicit human review.
            try:
                svc.start_plan(run['id'])
            except Exception as exc:
                svc._fail(run['id'], exc)
                raise
        return {'run_id': run['id'], 'duplicate': not created}

    from factory.control.project_routes import router
    app.include_router(router(store, svc))

    @app.get('/api/{path:path}')
    def legacy(path: str, request: Request):
        if path == 'runtime':
            raise HTTPException(404, '这是旧版模型配置接口。请刷新页面，使用当前工作台的“运行配置”。')
        if path.startswith('v2/') or path.startswith('auth/'):
            raise HTTPException(404, '接口不存在')
        if path == 'events':
            import threading
            from fastapi.responses import StreamingResponse
            from starlette.concurrency import run_in_threadpool
            from factory.events import live_stream, replay_stream
            audit = Path(os.getenv('FACTORY_AUDIT_DB', 'audit.db'))
            queue = Path(os.getenv('FACTORY_QUEUE', '~/.factory/q')).expanduser()
            stopped = threading.Event()
            replay = request.query_params.get('replay')
            iterator = (replay_stream(audit, replay, speed=0, stop=stopped.is_set) if replay
                        else live_stream(audit, queue, stop=stopped.is_set, sleep=stopped.wait))
            async def stream():
                try:
                    while auth.authenticate(request.cookies.get(COOKIE, '')):
                        if await request.is_disconnected():
                            break
                        event = await run_in_threadpool(lambda: next(iterator, None))
                        if event is None:
                            break
                        yield f"event: {event['type']}\ndata: {json.dumps(scrub(event), ensure_ascii=False)}\n\n"
                finally:
                    stopped.set()
            return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})
        from factory.api import route
        code, payload = route('/api/' + path + ('?' + request.url.query if request.url.query else ''),
            db=os.getenv('FACTORY_AUDIT_DB', 'audit.db'),
            queue=Path(os.getenv('FACTORY_QUEUE', '~/.factory/q')).expanduser())
        return JSONResponse(scrub(payload), status_code=code)

    @app.get('/{path:path}')
    def frontend(path: str):
        candidate = (static / path).resolve()
        if candidate.is_relative_to(static) and candidate.is_file():
            return FileResponse(candidate)
        if path.startswith('assets/'):
            raise HTTPException(404, '资源不存在')
        if (static / 'index.html').is_file():
            return FileResponse(static / 'index.html')
        return PlainTextResponse('前端尚未构建：在 frontend 执行 npm ci && npm run build', status_code=503)

    return app


def main():
    parser = argparse.ArgumentParser(description='webuddy authenticated gateway')
    sub = parser.add_subparsers(dest='command', required=True)
    user = sub.add_parser('create-user')
    user.add_argument('username')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8788)
    ns = parser.parse_args()
    if ns.command == 'create-user':
        data = Path(os.getenv('FACTORY_CONTROL_DATA', '~/.factory/control')).expanduser()
        password = getpass.getpass('Password (12+ characters): ')
        if password != getpass.getpass('Confirm password: '):
            parser.error('Passwords do not match')
        AuthStore(data / 'users.db').create_user(ns.username, password)
        print('User created.')
    else:
        import uvicorn
        # Loopback only; HTTPS reverse proxy is the public boundary.
        uvicorn.run(create_app(), host='127.0.0.1', port=ns.port, proxy_headers=False)


if __name__ == '__main__':
    main()
