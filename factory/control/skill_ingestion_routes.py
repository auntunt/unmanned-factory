"""Admin ingestion/review actions and project-authorized read views; no external scripts."""
import io
import hashlib
import json
import os
import stat
import zipfile
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from factory.control.agents import MAX_ZIP, MAX_PACK_FILES, MAX_FILES, MAX_FILE, macos_junk, inspect_skill, strip_macos_junk
from factory.control.skill_ingestion import validate_mapping
from factory.control.skill_ingestion_runs import authorization_snapshot, check_configuration
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
    selected_paths: list[str] | None = None


class Directory(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: str | None = None
    agent_id: str | None = None
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
                    if macos_junk(name):
                        continue
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, flags, dir_fd=parent)
                        try:
                            visit(child, prefix + name + '/')
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        count += 1
                        if count > MAX_PACK_FILES:
                            raise ValueError(f'目录文件数超过 {MAX_PACK_FILES}')
                        if info.st_size > MAX_FILE:
                            raise ValueError('Skill 单文件超过 2 MiB')
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

    def access(request, pid, aid=None):
        if pid:
            service.governance.require_project(request.state.user['id'], pid)
        else:
            if request.state.user.get('role') != 'admin':
                raise HTTPException(403, '职能体范围的适配仅管理员可管理')
            if not aid:
                raise HTTPException(422, '未选择项目时必须指定职能体 agent_id')
        if aid:
            service.agents.get(aid)

    def get(request, iid):
        try:
            record = ingestions.get(iid)
        except KeyError:
            raise HTTPException(404, '摄取记录不存在') from None
        access(request, record['project_id'], record.get('target_agent_id'))
        if record.get('target_agent_id'):
            manifest = service.agent_manifests.get(record['target_agent_id'])
            record['target_identity'] = manifest['identity']
            record['available_slots'] = max(0, 24 - len(manifest['skills']))
        if record.get('run_id'):
            run = service.store.get(record['run_id'])
            record['runtime'] = {key: run.get(key) for key in ('status', 'error', 'revision', 'resume_count')}
            record['progress'] = {'mapped': len(record.get('mapping_batches') or []),
                                  'verified': len(record.get('verification_batches') or []),
                                  'total': record.get('batch_count')}
        return record

    def create(request, pid, raw, aid=None):
        with service.lock:
            return create_locked(request, pid, raw, aid)

    def create_locked(request, pid, raw, aid=None):
        access(request, pid, aid)
        # Reuse resumable preparation; terminal jobs must permit a fresh upload.
        with service.store.connect() as db:
            existing = db.execute('''SELECT data FROM skill_ingestions
                WHERE project_id IS ? AND json_extract(data,'$.target_agent_id') IS ?
                AND json_extract(data,'$.source_sha256')=? ORDER BY rowid DESC LIMIT 1''',
                (pid, aid, hashlib.sha256(raw).hexdigest())).fetchone()
        if existing and json.loads(existing[0]).get('run_id'):
            previous = get(request, json.loads(existing[0])['id'])
            if previous['runtime']['status'] not in ('cancelled', 'failed'):
                return previous
        check_configuration(service)
        try:
            record = ingestions.create(pid, raw, request.state.user['id'], agent_id=aid)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        run, _ = service.store.create_run(pid, '职能包适配：只读分类、映射、独立验收后交人签',
            source={'type': 'skill_ingestion', 'skill_ingestion_id': record['id'], 'target_agent_id': aid,
                    'actor_id': request.state.user['id'], 'actor': request.state.user['username']})
        record = ingestions.update(record['id'], {'run_id': run['id']}, record['revision'], request.state.user['id'])
        try:
            service.start_plan(run['id'])
        except Exception as exc:
            service._fail(run['id'], exc)
            raise
        return record

    @api.get('/agents/{aid}/abilities/preflight')
    def preflight(aid: str, request: Request):
        access(request, None, aid)
        try:
            check_configuration(service)
        except (Conflict, ValueError) as exc:
            return {'ready': False, 'message': str(exc)}
        return {'ready': True, 'message': '上传后自动准备和验收，完成后核对启用。工具环境需单独验证。'}

    @api.post('/skill-ingestions', status_code=201)
    def upload(request: Request, project_id: str | None = Form(None),
               agent_id: str | None = Form(None), file: UploadFile = File(...)):
        return create(request, project_id, file.file.read(MAX_ZIP + 1), agent_id)

    def equip(request, aid, raw, filename):
        access(request, None, aid)
        try:
            meta = inspect_skill(raw, max_files=MAX_PACK_FILES)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        count = sum(PurePosixPath(f['path'].replace('\\', '/')).name.lower() == 'skill.md' for f in meta['files'])
        if count or len(meta['files']) > MAX_FILES:
            return {'channel': 'adaptation', 'skill_count': count, 'file_count': len(meta['files']),
                    'message': f'检测到 {count} 个 skill、{len(meta["files"])} 个文件，已进入自动准备；完成后在此核对启用，无需另开维护对话',
                    'ingestion': create(request, None, raw, aid)}
        sid = uuid.uuid4().hex
        asset = {**meta, 'id': sid, 'agent_id': aid, 'filename': filename,
                 'source': 'upload', 'created_at': now()}
        with service.store.connect() as db:
            db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)',
                       (sid, aid, json.dumps(asset, ensure_ascii=False), strip_macos_junk(raw), asset['created_at']))
        return {'channel': 'attachment', 'skill_count': count, 'file_count': len(meta['files']),
                'message': ('单 skill，直接添加' if count else '未发现 SKILL.md，保存为维护资料') + '为维护附件；在维护对话整理并应用后生效', 'asset': asset}

    @api.post('/agents/{aid}/abilities', status_code=201)
    def ability(aid: str, request: Request, file: UploadFile = File(...)):
        return equip(request, aid, file.file.read(MAX_ZIP + 1), file.filename or 'skill.zip')

    @api.post('/skill-ingestions/directory', status_code=201)
    def directory(body: Directory, request: Request):
        access(request, body.project_id, body.agent_id)
        try:
            # Admin provisions a dedicated mount root; never read arbitrary server paths.
            root = (service.store.project(body.project_id)['workspace'] if body.project_id
                    else os.environ.get('FACTORY_SKILL_IMPORT_DIR'))
            if not root:
                raise ValueError('尚未配置职能体资料挂载目录 FACTORY_SKILL_IMPORT_DIR，请上传 ZIP')
            raw = directory_zip(root, body.path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        return create(request, body.project_id, raw, body.agent_id)

    @api.get('/skill-ingestions')
    def records(request: Request, project_id: str | None = None, agent_id: str | None = None):
        access(request, project_id, agent_id)
        column = "json_extract(data,'$.target_agent_id')" if agent_id else 'project_id'
        with service.store.connect() as db:
            ids = [json.loads(row[0])['id'] for row in db.execute(
                f'SELECT data FROM skill_ingestions WHERE {column}=? ORDER BY rowid DESC LIMIT 50',
                (agent_id or project_id,))]
        return {'items': [get(request, iid) for iid in ids]}

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
                                   request.state.user['id'], service.agent_manifests, selected_paths=body.selected_paths)
        except Conflict:
            raise
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
