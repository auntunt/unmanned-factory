"""Admin ingestion/review actions and project-authorized read views; no external scripts."""
import io
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from factory.control.agents import MAX_ZIP, MAX_FILES, MAX_FILE
from factory.control.skill_ingestion import validate_mapping
from factory.control.skill_ingestion_runs import authorization_snapshot
from factory.control.store import Conflict, now


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    mapping: dict


class Sign(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    identity: str = Field(max_length=1200)
    authorization: dict[str, bool]


class Directory(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str
    path: str = Field(min_length=1, max_length=1000)


class Grant(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    targets: list[str] = Field(min_length=1, max_length=20)


def directory_zip(workspace, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('只接受项目工作区内的相对目录')
    out = io.BytesIO()
    count = 0
    total = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(Path(workspace), flags)
    try:
        for part in path.parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            def visit(parent, prefix=''):
                nonlocal count, total
                for name in sorted(os.listdir(parent)):
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, flags, dir_fd=parent)
                        try:
                            visit(child, prefix + name + '/')
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        count += 1
                        if count > MAX_FILES or info.st_size > MAX_FILE:
                            raise ValueError('目录文件数量或大小超出限额')
                        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        with os.fdopen(file_fd, 'rb') as stream:
                            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                raise ValueError('目录只接受普通文件')
                            raw = stream.read(MAX_FILE + 1)
                        total += len(raw)
                        if len(raw) > MAX_FILE or total > MAX_ZIP:
                            raise ValueError('目录内容超出限额')
                        archive.writestr(prefix + name, raw)
                    else:
                        raise ValueError('目录不得包含符号链接或特殊文件')
            visit(fd)
    finally:
        os.close(fd)
    return out.getvalue()


def router(service):
    api = APIRouter(prefix='/api/v4')
    ingestions = service.skill_ingestions

    def access(request, pid):
        service.governance.require_project(request.state.user['id'], pid)

    def get(request, iid):
        try:
            record = ingestions.get(iid)
        except KeyError:
            raise HTTPException(404, '摄取记录不存在') from None
        access(request, record['project_id'])
        return record

    def create(request, pid, raw):
        access(request, pid)
        try:
            record = ingestions.create(pid, raw, request.state.user['id'])
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        run, _ = service.store.create_run(pid, '职能包适配：只读分类、映射、独立验收后交人签',
            source={'type': 'skill_ingestion', 'skill_ingestion_id': record['id'],
                    'actor_id': request.state.user['id'], 'actor': request.state.user['username']})
        record = ingestions.update(record['id'], {'run_id': run['id']}, record['revision'], request.state.user['id'])
        try:
            service.start_plan(run['id'])
        except Exception as exc:
            service._fail(run['id'], exc)
            raise
        return record

    @api.post('/skill-ingestions', status_code=201)
    def upload(request: Request, project_id: str = Form(...), file: UploadFile = File(...)):
        return create(request, project_id, file.file.read(MAX_ZIP + 1))

    @api.post('/skill-ingestions/directory', status_code=201)
    def directory(body: Directory, request: Request):
        access(request, body.project_id)
        try:
            raw = directory_zip(service.store.project(body.project_id)['workspace'], body.path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        return create(request, body.project_id, raw)

    @api.get('/skill-ingestions')
    def records(request: Request, project_id: str):
        access(request, project_id)
        with service.store.connect() as db:
            return {'items': [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM skill_ingestions WHERE project_id=? ORDER BY rowid DESC LIMIT 50', (project_id,))]}

    @api.get('/skill-ingestions/{iid}')
    def detail(iid: str, request: Request):
        return get(request, iid)

    @api.put('/skill-ingestions/{iid}')
    def edit(iid: str, body: Review, request: Request):
        record = get(request, iid)
        if record['status'] != 'review':
            raise Conflict('只允许编辑待人签的已验收草稿')
        try:
            mapping = validate_mapping(ingestions.get(iid, package=True), body.mapping)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # Human edits remain review material; signature explicitly attests them.
        return ingestions.update(iid, {'mapping': mapping, 'human_edited': True}, body.revision, request.state.user['id'])

    @api.post('/skill-ingestions/{iid}/sign')
    def sign(iid: str, body: Sign, request: Request):
        get(request, iid)
        try:
            return ingestions.sign(iid, body.revision, body.identity, body.authorization,
                                   request.state.user['id'], service.agent_manifests)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.post('/runs/{rid}/skill-target-authorization')
    def authorize(rid: str, body: Grant, request: Request):
        with service.lock:
            run = service.store.get(rid)
            access(request, run['project_id'])
            if run['status'] != 'needs_human' or run['revision'] != body.revision or rid in service.active_jobs:
                raise Conflict('仅可为已暂停且未变更的运行授权')
            if any(not t.strip() or len(t) > 500 or '\n' in t for t in body.targets):
                raise HTTPException(422, '请明确列出每个目标')
            snapshot = authorization_snapshot(run)
            if not snapshot:
                raise Conflict('此运行没有需要目标授权的 skill')
            with service.store.connect() as db:
                db.execute('INSERT OR REPLACE INTO skill_target_authorizations VALUES(?,?,?,?,?)',
                    (rid, json.dumps(snapshot), json.dumps(body.targets), str(request.state.user['id']), now()))
            service.store.update(rid, {'authorized_skill_targets': body.targets},
                expected=('needs_human',), event=('skill.targets_authorized', {
                    'actor': request.state.user['id'], 'targets': body.targets, 'skills': snapshot}))
            if not run.get('plan'):
                service.store.update(rid, {'status': 'received', 'error': None}, expected=('needs_human',),
                                     event=('run.continued', {'actor': request.state.user['id']}))
                service.start_plan(rid)
            return {'run_id': rid, 'targets': body.targets}
    return api
