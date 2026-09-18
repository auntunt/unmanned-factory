"""会话级 Skill 绑定路由：POST/GET/DELETE 三端点。

路由前缀 /api/v4/sessions/{sid}/skills，与既有 skill-ingestions 同族命名。
不触碰 instruction_modules / skill_assets / project_modules / agent manifests。
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from factory.control.session_skills import SessionSkillStore
from factory.control.agents import MAX_ZIP


def router(store):
    api = APIRouter(prefix='/api/v4')
    session_skills = SessionSkillStore(store)

    def _verify_session(sid: str):
        """确认 session_id 对应已存在的 agent_conversations 行。"""
        with store.connect() as db:
            row = db.execute(
                'SELECT 1 FROM agent_conversations WHERE id=?', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, '会话不存在')

    @api.post('/sessions/{sid}/skills', status_code=201)
    def create_skill(sid: str, request: Request, file: UploadFile = File(...)):
        _verify_session(sid)
        actor_id = request.state.user['id']
        raw = file.file.read(MAX_ZIP + 1)
        if len(raw) > MAX_ZIP:
            raise HTTPException(422, 'Skill 包大小超过限制')
        try:
            record = session_skills.create(sid, raw, str(actor_id))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return record

    @api.get('/sessions/{sid}/skills')
    def list_skills(sid: str, request: Request):
        _verify_session(sid)
        return {'items': session_skills.list(sid)}

    @api.delete('/sessions/{sid}/skills/{skill_id}')
    def delete_skill(sid: str, skill_id: str, request: Request):
        _verify_session(sid)
        try:
            session_skills.delete(skill_id, sid)
        except KeyError:
            raise HTTPException(404, '绑定不存在') from None
        return {'deleted': True}

    return api
