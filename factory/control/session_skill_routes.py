"""会话级 Skill 绑定路由：POST/GET/DELETE 三端点。

路由前缀 /api/v4/sessions/{sid}/skills，与既有 skill-ingestions 同族命名。
不触碰 instruction_modules / skill_assets / project_modules / agent manifests。

POST 支持两种来源：
  1. multipart/form-data + file 字段 → ZIP 导入（N3 既有）
  2. application/json + origin="github" → GitHub 仓库拉取（N4）
"""
from __future__ import annotations

import json

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from factory.control.session_skills import SessionSkillStore
from factory.control.agents import MAX_ZIP


def router(store):
    api = APIRouter(prefix='/api/v4')
    session_skills = SessionSkillStore(store)

    def _verify_session_owner(sid: str, request: Request):
        """确认会话存在且当前用户有权访问（拥有者或管理员）。"""
        with store.connect() as db:
            row = db.execute(
                'SELECT data FROM agent_conversations WHERE id=?', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, '会话不存在')
        conv = json.loads(row[0])
        user = request.state.user
        if user.get('role') == 'admin':
            return
        if str(conv.get('actor_id')) != str(user['id']):
            raise HTTPException(403, '无权访问该会话')

    @api.post('/sessions/{sid}/skills', status_code=201)
    async def create_skill(sid: str, request: Request):
        _verify_session_owner(sid, request)
        actor_id = request.state.user['id']
        content_type = request.headers.get('content-type', '')

        if 'multipart/form-data' in content_type:
            return await _create_from_zip(sid, request, actor_id)
        else:
            return await _create_from_github(sid, request, actor_id)

    async def _create_from_zip(sid: str, request: Request, actor_id: str):
        """ZIP 导入路径（N3 既有逻辑）。"""
        form = await request.form()
        file = form.get('file')
        if not file:
            raise HTTPException(422, '缺少 file 字段')
        raw = await file.read()
        if len(raw) > MAX_ZIP:
            raise HTTPException(422, 'Skill 包大小超过限制')
        try:
            record = session_skills.create(sid, raw, str(actor_id))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return record

    async def _create_from_github(sid: str, request: Request, actor_id: str):
        """GitHub 仓库拉取路径（N4）。"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, '请求体格式无效，需要 JSON 或 multipart/form-data') from None

        if not isinstance(body, dict) or body.get('origin') != 'github':
            raise HTTPException(422, '不支持的 origin，当前仅支持 zip 和 github')

        owner_repo = body.get('owner_repo')
        if not owner_repo or not isinstance(owner_repo, str):
            raise HTTPException(422, '缺少 owner_repo 字段（格式：owner/repo）')

        ref = body.get('ref')
        subpath = body.get('subpath')

        # 获取 fetcher（可通过 app.state 注入，便于测试）
        fetcher = getattr(request.app.state, 'github_skill_fetcher', None)
        if fetcher is None:
            from factory.control.github_skill_fetch import GitHubSkillFetcher
            fetcher = GitHubSkillFetcher()

        from factory.control.github_skill_fetch import GitHubSkillFetchError
        try:
            result = fetcher.fetch(owner_repo, ref=ref, subpath=subpath)
        except GitHubSkillFetchError as exc:
            raise HTTPException(422, {
                'message': str(exc),
                'reason': exc.reason,
            }) from None

        record = session_skills.create_from_github(sid, result, str(actor_id))
        return record

    @api.get('/sessions/{sid}/skills')
    def list_skills(sid: str, request: Request):
        _verify_session_owner(sid, request)
        return {'items': session_skills.list(sid)}

    @api.delete('/sessions/{sid}/skills/{skill_id}')
    def delete_skill(sid: str, skill_id: str, request: Request):
        _verify_session_owner(sid, request)
        try:
            session_skills.delete(skill_id, sid)
        except KeyError:
            raise HTTPException(404, '绑定不存在') from None
        return {'deleted': True}

    return api
