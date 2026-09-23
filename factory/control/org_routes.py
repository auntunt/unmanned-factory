"""Organisation administration (admin only) and scoped management reads.

Writes are already admin-only at the middleware boundary for non-listed paths;
each handler checks again so the rule does not depend on that list alone.
"""
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from factory.control.auth import AuthError


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class UnitCreate(Body):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['company', 'department', 'group']
    parent_id: str | None = Field(default=None, max_length=64)


class UnitUpdate(Body):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    parent_id: str | None = Field(default=None, max_length=64)


class Binding(Body):
    unit_id: str = Field(min_length=1, max_length=64)


class ScopeGrant(Body):
    user_id: int = Field(ge=1, strict=True)
    unit_id: str = Field(min_length=1, max_length=64)


def _admin(request):
    user = request.state.user
    if user['role'] != 'admin':
        raise AuthError('此操作需要管理员权限', 403)
    return user['username']


def router(org):
    api = APIRouter()

    @api.get('/api/v5/me/workspaces')
    def me(request: Request):
        return org.me(request.state.user)

    @api.get('/api/v5/org')
    def tree(request: Request):
        _admin(request)
        return org.tree()

    @api.post('/api/v5/org/units', status_code=201)
    def create_unit(body: UnitCreate, request: Request):
        return org.create_unit(body.name, body.kind, body.parent_id, _admin(request))

    @api.patch('/api/v5/org/units/{unit_id}')
    def update_unit(unit_id: str, body: UnitUpdate, request: Request):
        return org.update_unit(unit_id, _admin(request), name=body.name, parent_id=body.parent_id,
                               move='parent_id' in body.model_fields_set)

    @api.delete('/api/v5/org/units/{unit_id}')
    def delete_unit(unit_id: str, request: Request):
        org.delete_unit(unit_id, _admin(request))
        return {'ok': True}

    @api.put('/api/v5/org/projects/{project_id}')
    def bind(project_id: str, body: Binding, request: Request):
        org.bind_project(project_id, body.unit_id, _admin(request))
        return {'ok': True}

    @api.delete('/api/v5/org/projects/{project_id}')
    def unbind(project_id: str, request: Request):
        org.unbind_project(project_id, _admin(request))
        return {'ok': True}

    @api.post('/api/v5/org/scopes', status_code=201)
    def grant(body: ScopeGrant, request: Request):
        org.grant_scope(body.user_id, body.unit_id, _admin(request))
        return {'ok': True}

    @api.delete('/api/v5/org/scopes/{user_id}/{unit_id}')
    def revoke(user_id: int, unit_id: str, request: Request):
        org.revoke_scope(user_id, unit_id, _admin(request))
        return {'ok': True}

    @api.get('/api/v5/management/overview')
    def overview(request: Request, unit_id: str | None = Query(default=None, max_length=64),
                 days: int = Query(default=30, ge=1, le=90)):
        return org.overview(request.state.user, unit_id, days)

    @api.get('/api/v5/management/projects/{project_id}')
    def project(project_id: str, request: Request):
        return org.project_detail(request.state.user, project_id)

    return api
