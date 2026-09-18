"""Authenticated API for the capability-pack lifecycle.

Prefix `/api/v4/capability-packs`, chosen after checking the existing routes: `/api/v3/
capabilities` is the distilled-method library and stays untouched, and this is a different
entity — a versioned, executable capability.

Every state change that matters is decided here on the server: who may maintain a pack,
whether the candidate is still the one that was validated, whether an evaluation passed,
which version a task freezes, and whether the caller owns the file being read. The client
never supplies the actor, the provider, or another user's artifact id and gets away with it.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from factory.control.capability_packs import MAX_ARTIFACT_BYTES, PackStore
from factory.control.pack_runtime import environment_report, evaluate, run_tool
from factory.control.pack_zip import build_tool_zip
from factory.control.store import Conflict, now

KEY = r'^[A-Za-z0-9_-]{8,100}$'


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Selection(Body):
    name: StrictStr = Field(min_length=1, max_length=255)
    material_scope: StrictStr | None = Field(default=None, pattern='^(synthetic|licensed)$')


class DraftFromTask(Body):
    source_run_id: StrictStr = Field(min_length=1, max_length=200)
    selections: list[Selection] = Field(min_length=1, max_length=200)
    operation_key: StrictStr = Field(pattern=KEY)


class DraftFile(Body):
    path: StrictStr = Field(min_length=1, max_length=255)
    content: StrictStr = Field(max_length=2_000_000)
    role: StrictStr = Field(default='program', pattern='^(program|fixture)$')
    material_scope: StrictStr | None = Field(default=None, pattern='^(synthetic|licensed)$')


class DraftUpdate(Body):
    expected_revision: StrictInt = Field(ge=1)
    files: list[DraftFile] = Field(min_length=1, max_length=200)


class RunEvaluation(Body):
    expected_revision: StrictInt = Field(ge=1)
    operation_key: StrictStr = Field(pattern=KEY)


class Publish(Body):
    expected_revision: StrictInt = Field(ge=1)
    evaluation_id: StrictStr = Field(min_length=8, max_length=64)
    operation_key: StrictStr = Field(pattern=KEY)


class BindVersion(Body):
    agent_id: StrictStr = Field(min_length=1, max_length=200)
    version_id: StrictStr = Field(min_length=8, max_length=64)
    expected_revision: StrictInt = Field(ge=0)


def router(store, service):
    api = APIRouter(prefix='/api/v4/capability-packs')
    packs = PackStore(store)

    def job_state(job_id):
        if not job_id:
            return None
        try:
            return service.maintenance_status(job_id)['status']
        except KeyError:
            return None

    # 服务重启后把在途调用对齐到明确终态：Service 会把 maintenance_jobs 标成 interrupted，
    # pack_tasks 不跟着对账的话，界面会永远停在「处理中」。
    packs.recover_interrupted(job_status=job_state)

    def actor(request: Request):
        return request.state.user

    def guarded(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict:
            raise
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except KeyError:
            raise HTTPException(404, '记录不存在') from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def run_items(rid, request):
        """Development output for a run: its saved, immutable deliverables. Access is
        checked against the run's project, so a pack can never be cut from someone
        else's task."""
        run = guarded(store.get, rid)
        if service.governance:
            guarded(service.governance.require_project, actor(request)['id'], run['project_id'])
        sha = (run.get('artifacts') or {}).get('commit', '')
        if not (isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha)):
            raise HTTPException(409, '该任务还没有保存成果，无法沉淀为职能包。')
        base = Path(store.path).parent / 'deliverables' / rid / sha
        if not (base / 'manifest.json').is_file():
            raise HTTPException(409, '该任务还没有保存成果，请先保存成果再沉淀能力。')
        manifest = json.loads((base / 'manifest.json').read_text())
        items = []
        with zipfile.ZipFile(base / 'files.zip') as archive:
            for item in manifest['items']:
                if item['size'] > 2 * 1024 * 1024:
                    items.append({**item, 'content': b''})
                    continue
                items.append({**item, 'content': archive.read(item['name'])})
        return items

    # ---- catalogue ---------------------------------------------------------
    @api.get('')
    def list_packs(request: Request):
        rows = packs.list()
        uid = str(actor(request)['id'])
        admin = actor(request).get('role') == 'admin'
        # Unpublished candidates are only visible to their maintainer; a published pack is
        # visible to the workspace (publishing is an internal registration, not a deploy).
        return {'packs': [p for p in rows if p.get('published_version') or p['owner_id'] == uid or admin]}

    # ---- candidate ---------------------------------------------------------
    @api.post('/drafts', status_code=201)
    def create_draft(body: DraftFromTask, request: Request):
        items = run_items(body.source_run_id, request)
        return guarded(packs.create_draft,
                       source={'kind': 'run', 'id': body.source_run_id, 'items': items},
                       selections=[s.model_dump() for s in body.selections],
                       actor=actor(request), operation_key=body.operation_key)

    @api.put('/{pack_id}/draft')
    def update_draft(pack_id: str, body: DraftUpdate, request: Request):
        return guarded(packs.update_draft, pack_id, expected_revision=body.expected_revision,
                       files=[{**f.model_dump(), 'content': f.content.encode()} for f in body.files],
                       actor=actor(request))

    # ---- evaluation --------------------------------------------------------
    @api.post('/{pack_id}/evaluations', status_code=202)
    def start_evaluation(pack_id: str, body: RunEvaluation, request: Request):
        detail = guarded(packs.get, pack_id)
        if detail['owner_id'] != str(actor(request)['id']) and actor(request).get('role') != 'admin':
            raise HTTPException(403, '只有能力维护者可以运行验证')
        draft = guarded(packs.draft, pack_id)
        if int(draft['revision']) != int(body.expected_revision):
            raise Conflict('候选内容已被更新，请刷新后重新运行验证')
        if not draft.get('manifest'):
            raise HTTPException(409, draft.get('blocked_reason') or '工具契约不完整，无法验证')
        files = guarded(packs.files, pack_id=pack_id)
        digest, who = draft['content_digest'], actor(request)
        job_id = uuid.uuid4().hex
        # 提交即登记幂等键：同键重复提交回放同一个作业，而不是把测试集再真跑一遍。
        # 同键不同候选内容 → Conflict（由 _replay 抛出）。
        registered = guarded(packs.begin_evaluation, pack_id, content_digest_value=digest,
                             actor=who, operation_key=body.operation_key, job_id=job_id)
        replay = bool(registered.get('idempotent_replay'))
        if replay:
            replayed = registered.get('job_id')
            state = job_state(replayed)
            if state is not None:
                return {'job_id': replayed, 'status': state, 'content_digest': digest,
                        'idempotent_replay': True}
            # 登记成功但崩在派发之前：作业**根本不存在**。以前这里 `or 'completed'`，
            # 于是重放会报一个查不到的作业已完成。改成用登记的 job_id 真正补派发。
            job_id = replayed or job_id

        def job(cancel):
            report = evaluate(draft, files, cancel=cancel)
            if cancel.is_set():
                return {'status': 'cancelled'}
            # 幂等键已在提交时登记（begin_evaluation），这里不再覆盖那条登记——
            # 覆盖会让后续重放拿到的记录少掉 job_id，重试反而 404。
            record = packs.save_evaluation(pack_id, content_digest_value=digest, report=report,
                                           actor=who)
            return {'evaluation_id': record['id'], 'passed': record['passed'], 'summary': record['summary']}

        started = service.start_maintenance(job, job_id=job_id, conversation_id=f'pack-eval:{pack_id}',
                                            actor_id=who['id'])
        return {'job_id': started['id'], 'status': started['status'], 'content_digest': digest,
                'idempotent_replay': replay}

    @api.get('/jobs/{job_id}')
    def evaluation_job(job_id: str, request: Request):
        job = guarded(service.maintenance_status, job_id)
        if str(job.get('actor_id')) != str(actor(request)['id']) and actor(request).get('role') != 'admin':
            raise HTTPException(403, '无权查看该作业')
        return job

    # ---- publish -----------------------------------------------------------
    @api.post('/{pack_id}/versions', status_code=201)
    def publish(pack_id: str, body: Publish, request: Request):
        version = guarded(packs.publish, pack_id, expected_revision=body.expected_revision,
                          evaluation_id=body.evaluation_id, actor=actor(request),
                          operation_key=body.operation_key)
        # An availability check is recorded immediately, but it is a separate state:
        # `published` never implies the dependencies exist on this host.
        guarded(packs.record_env_check, version['id'], environment_report(version['manifest']))
        return version

    @api.post('/versions/{version_id}/environment-check')
    def check_environment(version_id: str, request: Request):
        version = guarded(packs.version, version_id)
        return guarded(packs.record_env_check, version_id, environment_report(version['manifest']))

    # ---- binding -----------------------------------------------------------
    @api.get('/bindings/{agent_id}')
    def agent_bindings(agent_id: str):
        return {'bindings': packs.bindings(agent_id)}

    @api.post('/bindings', status_code=201)
    def bind(body: BindVersion, request: Request):
        return guarded(packs.bind, body.agent_id, body.version_id,
                       expected_revision=body.expected_revision, actor=actor(request))

    @api.delete('/bindings/{agent_id}/{pack_id}')
    def unbind(agent_id: str, pack_id: str, request: Request):
        return guarded(packs.unbind, agent_id, pack_id, actor=actor(request))

    # ---- invocation --------------------------------------------------------
    @api.post('/invocations', status_code=202)
    def invoke(request: Request, agent_id: str = Form(...), pack_id: str = Form(...),
               operation_key: str = Form(...), file: UploadFile = File(...)):
        if not re.fullmatch(KEY, operation_key or ''):
            raise HTTPException(422, '操作键格式不合法')
        raw = file.file.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise HTTPException(413, '文件超过 16 MB 上限')
        who = actor(request)
        artifact = guarded(packs.put_artifact, actor_id=who['id'], name=file.filename or 'input.bin',
                           content=raw, role='input', validation_status='not_applicable', dedupe=True)
        job_id = uuid.uuid4().hex
        task = guarded(packs.create_task, actor=who, agent_id=agent_id, pack_id=pack_id,
                       input_artifact_ids=[artifact['id']], operation_key=operation_key, job_id=job_id)
        current = packs.task(task['id'], actor=who)
        if task.get('idempotent_replay'):
            # 重放：只有当这次调用**从未真正派发**（作业不存在且任务仍排队）时才补派发，
            # 否则重试会把同一个文件再转一遍。
            if not (current['status'] == 'queued' and job_state(current.get('job_id')) is None):
                return current
            job_id = current.get('job_id') or job_id
        version = guarded(packs.version, task['snapshot']['version_id'])
        files = guarded(packs.files, version_id=version['id'])
        inputs = [{'name': artifact['name'], 'content': raw, 'sha256': artifact['sha256']}]

        def job(cancel):
            packs.update_task(task['id'], {'status': 'running'}, event=('task.running', {}),
                              expected=('queued', 'cancel_requested'))
            if cancel.is_set():
                packs.update_task(task['id'], {'status': 'cancelled', 'error_code': 'cancelled',
                                               'error': '调用已取消'}, event=('task.cancelled', {}))
                return {'status': 'cancelled'}
            outcome = run_tool(version, files, inputs, cancel=cancel)
            saved = [packs.put_artifact(actor_id=who['id'], name=out['name'], content=out['content'],
                                        role='output', task_id=task['id'], kind=out['kind'],
                                        validation_status=outcome['validation_status'])
                     for out in outcome['outputs']]
            packs.update_task(task['id'], {
                'status': outcome['status'], 'outputs': saved, 'error_code': outcome.get('error_code'),
                'error': outcome.get('error'), 'result': outcome.get('result'),
                'diagnostics': outcome.get('diagnostics', []), 'evidence': outcome['evidence'],
                'validation_status': outcome['validation_status'], 'duration_ms': outcome.get('duration_ms')},
                event=('task.' + outcome['status'], {'error_code': outcome.get('error_code'),
                                                     'outputs': [a['id'] for a in saved]}))
            return {'task_id': task['id'], 'status': outcome['status']}

        service.start_maintenance(job, job_id=job_id, conversation_id=f'pack-task:{task["id"]}',
                                  actor_id=who['id'])
        return packs.task(task['id'], actor=who)

    @api.post('/invocations/{task_id}/cancel')
    def cancel_invocation(task_id: str, request: Request):
        """取消是持久化的：先落库，再让作业层置取消位——工具进程组由运行时回收。"""
        task = guarded(packs.request_cancel, task_id, actor=actor(request))
        if task.get('job_id'):
            try:
                service.cancel_maintenance(task['job_id'], str(actor(request)['id']))
            except KeyError:
                pass
        return guarded(packs.task, task_id, actor=actor(request))

    @api.get('/invocations')
    def list_invocations(request: Request):
        return {'tasks': packs.tasks_for(actor(request))}

    @api.get('/invocations/{task_id}')
    def read_invocation(task_id: str, request: Request):
        return guarded(packs.task, task_id, actor=actor(request))

    @api.get('/invocations/{task_id}/events')
    def invocation_events(task_id: str, request: Request, cursor: int = 0):
        return guarded(packs.task_events, task_id, cursor=cursor, actor=actor(request))

    @api.get('/artifacts/{artifact_id}/download')
    def download_artifact(artifact_id: str, request: Request):
        body, content = guarded(packs.artifact, artifact_id, actor=actor(request), with_content=True)
        # A Chinese filename must survive the header: latin-1 only in `filename`, the real
        # name in RFC 5987 `filename*`. Encoding the raw name would raise and lose the file.
        name = body['name'].replace('"', '')
        ascii_name = name.encode('ascii', 'replace').decode('ascii')
        quoted = quote(name, safe='')
        return Response(content, media_type='application/octet-stream', headers={
            'Content-Disposition': f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}',
            'X-Content-Type-Options': 'nosniff',
            # The label travels with the file: a candidate output that failed validation is
            # downloadable, but never presented as verified.
            'X-Validation-Status': body['validation_status']})


    # ---- 工具执行包下载 ---------------------------------------------------
    @api.get('/versions/{version_id}/tool-pack')
    def download_tool_pack(version_id: str, request: Request):
        """下载可独立运行的工具执行包（含 CLI harness、版本标识、sha256 校验文件）。

        与 agent-pack ZIP（模板包）不同，这是用户能在平台外直接
        `python cli.py --input x --output y` 运行的那一份。
        """
        version = guarded(packs.version, version_id)
        files = guarded(packs.files, version_id=version_id)
        raw = build_tool_zip(version, files)
        zip_sha256 = hashlib.sha256(raw).hexdigest()
        slug = version.get('manifest', {}).get('tool', {}).get('name', 'tool')
        filename = f'{slug}-v{version["version"]}.zip'
        return Response(raw, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Content-Type-Options': 'nosniff',
            'X-Pack-Version': str(version['version']),
            'X-Pack-SHA256': zip_sha256,
        })

    # ---- 动态段放最后 ----------------------------------------------------
    # `/{pack_id}` 会吞掉任何在它之后声明的同层静态路径（实测 GET /invocations 返回 404）。
    # FastAPI 按声明顺序匹配，所以静态路径一律先声明，带路径参数的放最后。
    @api.get('/{pack_id}')
    def read_pack(pack_id: str, request: Request):
        detail = guarded(packs.get, pack_id)
        uid, admin = str(actor(request)['id']), actor(request).get('role') == 'admin'
        maintainer = detail['owner_id'] == uid or admin
        if not maintainer and not detail.get('versions'):
            raise HTTPException(403, '无权查看该职能包')
        if not maintainer:
            detail = {**detail, 'draft': None}
        return {**detail, 'can_maintain': maintainer}

    @api.get('/{pack_id}/files/{path:path}')
    def read_file(pack_id: str, path: str, request: Request, version_id: str | None = None):
        detail = guarded(packs.get, pack_id)
        if detail['owner_id'] != str(actor(request)['id']) and actor(request).get('role') != 'admin':
            raise HTTPException(403, '无权查看该职能包内容')
        files = guarded(packs.files, pack_id=pack_id, version_id=version_id)
        if path not in files:
            raise HTTPException(404, '文件不存在')
        return Response(files[path], media_type='text/plain; charset=utf-8',
                        headers={'X-Content-Type-Options': 'nosniff'})


    return api
