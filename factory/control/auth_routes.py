"""Authentication endpoints; authentication/CSRF/admin middleware stays in app.py."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from factory.control.team_routes import Password

COOKIE = 'factory_session'

class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(Body):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=4096)
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)


def router(auth, governance, parsed_origin):
    api = APIRouter()

    @api.post('/api/auth/login')
    def login(body: Login):
        token, csrf, user = auth.login(body.username, body.password)
        response = JSONResponse({'user': user, 'csrf_token': csrf})
        response.set_cookie(COOKIE, token, httponly=True, secure=parsed_origin.scheme == 'https',
                            samesite='strict', max_age=43200, path='/')
        return response


    @api.get('/api/auth/me')
    def me(request: Request):
        user = request.state.user
        return {'user': {key: user[key] for key in ('id', 'username', 'role', 'active')}, 'csrf_token': user['csrf_token']}


    @api.post('/api/auth/logout')
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE, ''))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path='/')
        return response


    @api.post('/api/auth/password')
    def password(body: Password, request: Request):
        auth.change_password(request.state.user['id'], body.current_password, body.password)
        governance.audit(request.state.user['username'], 'member.password_changed', {'id': request.state.user['id']})
        return {'ok': True, 'login_required': True}

    return api
