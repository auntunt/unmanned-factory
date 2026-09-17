"""Admin-only target registration and invocation; member runs use trusted coordinator hooks."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from factory.control.store import Conflict


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Target(Body):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=64)
    host_fingerprint: str
    commands: dict[str, str]


class Update(Target):
    revision: int = Field(ge=1)


class Revision(Body):
    revision: int = Field(ge=1)


class Binding(Body):
    revision: int = Field(ge=0)
    targets: list[str] = Field(max_length=8)


class Invoke(Body):
    target_id: str
    verb: str
    lines: int = Field(default=100, ge=1, le=500)


def router(service):
    routes = APIRouter()
    targets, remote = service.targets, service.remote

    def admin(request):
        if request.state.user['role'] != 'admin':
            raise HTTPException(403, '此操作需要管理员权限')

    def invoke(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(404, '记录不存在') from None
        except ValueError as exc:
            if isinstance(exc, Conflict):
                raise
            raise HTTPException(422, str(exc)) from None

    @routes.get('/api/v2/deploy-targets')
    def listing(request: Request):
        admin(request)
        return {'targets': targets.list()}

    @routes.get('/api/v2/deploy-targets/checks')
    def last_checks(request: Request):
        admin(request)
        return {'checks': targets.last_checks()}

    @routes.post('/api/v2/deploy-targets', status_code=201)
    def create(body: Target, request: Request):
        admin(request)
        return invoke(targets.create, body.model_dump(), request.state.user['id'])

    @routes.get('/api/v2/deploy-targets/{tid}')
    def get(tid: str, request: Request):
        admin(request)
        return invoke(targets.get, tid)

    @routes.put('/api/v2/deploy-targets/{tid}')
    def update(tid: str, body: Update, request: Request):
        admin(request)
        return invoke(targets.update, tid, body.model_dump(exclude={'revision'}), body.revision, request.state.user['id'])

    @routes.delete('/api/v2/deploy-targets/{tid}')
    def delete(tid: str, body: Revision, request: Request):
        admin(request)
        invoke(targets.delete, tid, body.revision, request.state.user['id'])
        return {'deleted': True}

    @routes.post('/api/v2/deploy-targets/{tid}/test')
    def test(tid: str, request: Request):
        admin(request)
        return remote.test(tid, request.state.user['id'])

    @routes.get('/api/v2/projects/{pid}/deploy-targets')
    def bindings(pid: str, request: Request):
        service.governance.require_project(request.state.user['id'], pid)
        return {**targets.bindings(pid), 'available': targets.snapshot(pid)}

    @routes.put('/api/v2/projects/{pid}/deploy-targets')
    def bind(pid: str, body: Binding, request: Request):
        admin(request)
        return invoke(targets.bind, pid, body.targets, body.revision, request.state.user['id'])

    @routes.post('/api/v2/runs/{rid}/remote')
    def execute(rid: str, body: Invoke, request: Request):
        admin(request)
        try:
            result = remote.execute(rid, body.target_id, body.verb, lines=body.lines)
        except ValueError as exc:
            if isinstance(exc, Conflict):
                raise
            raise HTTPException(422, str(exc)) from None
        with service.lock:
            run = service.store.get(rid)
            artifacts = {**run['artifacts'], 'remote_results': [*run['artifacts'].get('remote_results', []), result]}
            service.store.update(rid, {'artifacts': artifacts})
        return result

    return routes
