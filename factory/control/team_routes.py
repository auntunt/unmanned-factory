"""Small trusted-team administration; all mutations require admin at boundary."""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class MemberCreate(Body):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=4096)
    role: Literal['admin', 'member'] = 'member'


class MemberUpdate(Body):
    role: Literal['admin', 'member']
    active: bool = Field(strict=True)
    password: str | None = Field(default=None, min_length=12, max_length=4096)


class Assignments(Body):
    project_ids: list[str] = Field(max_length=1000)


class Limit(Body):
    scope: Literal['member', 'project', 'workspace']
    scope_id: str = Field(min_length=1, max_length=100)
    limit_tokens: int | None = Field(ge=0, le=1_000_000_000_000, strict=True)


class Settings(Body):
    reservation_tokens: int = Field(ge=1, le=1_000_000, strict=True)


class Reconcile(Body):
    actual_tokens: int = Field(ge=0, le=1_000_000_000_000, strict=True)
    reason: str = Field(min_length=3, max_length=2000)


class Password(Body):
    current_password: str = Field(min_length=1, max_length=4096)
    password: str = Field(min_length=12, max_length=4096)


def router(auth, governance):
    api = APIRouter()

    @api.get('/api/v3/team')
    def team(request: Request):
        return governance.summary(request.state.user)

    @api.post('/api/v3/team/members', status_code=201)
    def create_member(body: MemberCreate, request: Request):
        user = auth.create_user(body.username, body.password, role=body.role)
        governance.audit(request.state.user['username'], 'member.created', user)
        return user

    @api.put('/api/v3/team/members/{user_id}')
    def update_member(user_id: int, body: MemberUpdate, request: Request):
        user = auth.update_user(user_id, **body.model_dump())
        governance.audit(request.state.user['username'], 'member.updated', {**user, 'password_reset': body.password is not None})
        return user

    @api.put('/api/v3/team/members/{user_id}/projects')
    def assign(user_id: int, body: Assignments, request: Request):
        governance.assign(user_id, body.project_ids, request.state.user['username'])
        return {'ok': True}

    @api.put('/api/v3/team/quotas')
    def quota(body: Limit, request: Request):
        governance.set_limit(body.scope, body.scope_id, body.limit_tokens, request.state.user['username'])
        return {'ok': True}

    @api.put('/api/v3/team/settings')
    def settings(body: Settings, request: Request):
        governance.set_reservation(body.reservation_tokens, request.state.user['username'])
        return {'ok': True}

    @api.post('/api/v3/team/calls/{call_id}/reconcile')
    def reconcile(call_id: str, body: Reconcile, request: Request):
        return governance.reconcile(call_id, body.actual_tokens, body.reason, request.state.user['username'])

    @api.post('/api/auth/password')
    def password(body: Password, request: Request):
        auth.change_password(request.state.user['id'], body.current_password, body.password)
        governance.audit(request.state.user['username'], 'member.password_changed', {'id': request.state.user['id']})
        return {'ok': True, 'login_required': True}

    return api
