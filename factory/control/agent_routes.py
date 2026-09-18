"""Authenticated agent and two-mode conversation API."""
from __future__ import annotations
import json
import tempfile
import uuid
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, ConfigDict, Field
from factory.control.agents import AgentStore, inspect_skill, MAX_FILES, strip_macos_junk
from factory.control.store import Conflict

class Body(BaseModel): model_config=ConfigDict(extra='forbid')
class AgentCreate(Body):
    name:str=Field(min_length=1,max_length=120); purpose:str=Field(default='',max_length=4000)
    identity:str|None=Field(default=None,max_length=1200)
    instructions:str=Field(default='',max_length=30000); model_settings:dict|None=None
    tool_scope:list[str]=Field(default_factory=list); acceptance:list[str]=Field(default_factory=list); delivery:dict|None=None
class ConversationCreate(Body): mode:str=Field(pattern='^(do|maintain)$'); project_id:str|None=None; client_key:str|None=Field(default=None,min_length=8,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
class RouteAttachment(Body):
    name:str=Field(default='',max_length=300); size:int|None=None; type:str|None=None
class RouteRequest(Body):
    text:str=Field(default='',max_length=50000)
    agent_id:str|None=None
    attachments:list[RouteAttachment]=Field(default_factory=list)
class Message(Body):
    content:str=Field(min_length=1,max_length=50000)
    idempotency_key:str|None=Field(default=None,min_length=8,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
_DEC=r'^\d{1,15}(\.\d{1,6})?$'  # explicit non-negative decimal string; magnitude/range checked in quote_calc
class QuoteItem(Body):
    name:str=Field(default='',max_length=200)
    unit_price:str=Field(pattern=_DEC)
    quantity:str=Field(pattern=_DEC)
class QuoteCalc(Body):
    items:list[QuoteItem]=Field(min_length=1,max_length=500)
    discount_rate:str=Field(default='1',pattern=_DEC)
class ExportDoc(Body):
    title:str=Field(default='',max_length=200)
    format:str=Field(pattern='^(md|txt|csv)$')
    content:str=Field(min_length=1,max_length=200000)
class DraftPatch(Body): expected_revision:int=Field(ge=0); patch:dict
class Apply(Body): expected_revision:int=Field(ge=0); idempotency_key:str|None=None
class Rollback(Body): version:int=Field(ge=1)
class ProjectHelper(Body):
    agent_id: str | None = None
    expected_revision: int = Field(ge=0)
class ModuleBody(Body):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(pattern='^(style|knowledge|workflow|delivery)$')
    description: str = Field(default='', max_length=1000)
    instructions: str = Field(min_length=1, max_length=16000)
    source_refs: list[dict] = Field(default_factory=list, max_length=12)
    source_slots: list[str] = Field(default_factory=list, max_length=12)
class ModuleUpdate(ModuleBody):
    expected_revision: int = Field(ge=1, strict=True)
class ModuleRef(Body):
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1, strict=True)
class ModuleSelection(Body):
    modules: list[ModuleRef] = Field(max_length=12)
    expected_revision: int = Field(ge=0, strict=True)

class LearningChoice(Body):
    source_id: str
    source_revision: int = Field(ge=1)
    destination: str = Field(pattern='^(standalone|agent|skip)$')
    agent_id: str | None = None
    draft_revision: int = Field(default=0, ge=0)


def router(store, service):
    api=APIRouter(prefix='/api/v4'); agents=AgentStore(store)
    def actor(req): return req.state.user
    def guarded(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except Conflict: raise
        except KeyError: raise HTTPException(404,'记录不存在')
        except ValueError as e: raise HTTPException(400,str(e)) from None
    def _append_pending(cid, content, job_id):
        return agents.append_message(cid, 'assistant', content, status='pending', job_id=job_id)
    def _json_model(text):
        raw=str(text).strip()
        if raw.startswith('```'):
            raw=raw.split('\n',1)[1] if '\n' in raw else raw
            raw=raw.rsplit('```',1)[0].strip()
        return json.loads(raw)

    from factory.control.project_assistants import ProjectAssistants
    helpers = ProjectAssistants(store)

    from factory.control.modules import ModuleStore
    modules = ModuleStore(store)
    from factory.control.sources import SourceStore
    sources = SourceStore(store)

    @api.get('/projects/{pid}/sources')
    def project_sources(pid: str, request: Request):
        if service.governance:
            service.governance.require_project(actor(request)['id'], pid)
        return {'sources': guarded(sources.list, pid)}

    @api.get('/runs/{rid}/mounts')
    def run_mounts(rid: str, request: Request):
        from factory.control.mounts import manifest_summary
        run = guarded(store.get, rid)
        if service.governance:
            service.governance.require_project(actor(request)['id'], run['project_id'])
        snapshot = run.get('mount_snapshot')
        return {'mount': manifest_summary(snapshot) if snapshot else None}

    from factory.control.agent_packs import install_builtins, pack_zip
    install_builtins(store)
    manifests = service.agent_manifests
    manifests.migrate_all()

    @api.get('/builtin-packs/{slug}/download')
    def download_builtin_pack(slug: str):
        from fastapi.responses import Response
        raw = guarded(pack_zip, slug)
        return Response(raw, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="{slug}.zip"',
            'X-Content-Type-Options': 'nosniff',
        })

    @api.get('/modules')
    def list_modules(agent_id: str | None = None):
        refs = manifests.references()
        return {'modules': [{**m, 'agent_references': refs.get(m['id'], []), 'agent_reference_count': len(refs.get(m['id'], []))} for m in modules.list(agent_id)]}

    @api.post('/modules', status_code=201)
    def create_module(body: ModuleBody, request: Request):
        return guarded(modules.save, body.model_dump(), actor(request)['id'])

    @api.put('/modules/{mid}')
    def update_module(mid: str, body: ModuleUpdate, request: Request):
        return guarded(modules.save, body.model_dump(exclude={'expected_revision'}), actor(request)['id'], mid, body.expected_revision)

    @api.get('/projects/{pid}/modules')
    def get_modules(pid: str):
        return guarded(modules.selection, pid)

    @api.put('/projects/{pid}/modules')
    def select_modules(pid: str, body: ModuleSelection, request: Request):
        return guarded(modules.select, pid, [m.model_dump() for m in body.modules], body.expected_revision, actor(request)['id'])

    @api.get('/projects/{pid}/assistant')
    def project_assistant(pid: str):
        return guarded(helpers.binding, pid)

    @api.put('/projects/{pid}/assistant')
    def bind_assistant(pid: str, body: ProjectHelper, request: Request):
        return guarded(helpers.bind, pid, body.agent_id, body.expected_revision, actor(request)['id'])

    @api.get('/projects/{pid}/learnings')
    def project_learnings(pid: str):
        return {'learnings': guarded(helpers.learnings, pid)}

    @api.post('/projects/{pid}/learnings/settle')
    def settle_learning(pid: str, body: LearningChoice, request: Request):
        with service.lock:
            return guarded(helpers.settle, pid, body.source_id, body.source_revision, body.destination,
                           body.agent_id, body.draft_revision, actor(request)['id'])

    @api.get('/projects/{pid}/learnings/export')
    def export_learning(pid: str, source_id: str):
        from fastapi.responses import Response
        entries = guarded(helpers.learnings, pid)
        item = next((e for e in entries if e['id'] == source_id), None)
        if not item or not item.get('disposition') or item['disposition']['destination'] != 'standalone':
            raise HTTPException(404, '尚未单独沉淀这条收获')
        source = item['disposition']['source']
        import hashlib
        name = 'project-learning-' + hashlib.sha256(source_id.encode()).hexdigest()[:12]
        description = json.dumps('适用于：' + source['title'], ensure_ascii=False)
        content = f"---\nname: {name}\ndescription: {description}\n---\n\n# {source['title']}\n\n{source['content']}\n\n来源项目：{pid}\n来源记录：{source_id} · 修订 {source['revision']}\n"
        import io, zipfile
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('SKILL.md', content)
            archive.writestr('source.json', json.dumps({'project_id': pid, 'source_id': source_id, **source}, ensure_ascii=False))
        return Response(output.getvalue(), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{name}.zip"'})

    @api.get('/agents')
    def list_agents(): return {'agents':agents.list()}
    @api.post('/route')
    def route_entry(body:RouteRequest,request:Request):
        # Server-side entry routing. The actor, the available roles and each role's
        # real chat capability come from the server, never from the client; this
        # endpoint creates nothing — it only decides. A role is chat-capable only when
        # its EFFECTIVE provider is claude (the one that consumes the session tools);
        # a codex/dsh-configured role is not auto-routed into a chat that would fail.
        # The MFD role is reported honestly as not-yet-executable (no converter).
        from factory.control import routing
        mfd_id=uuid.uuid5(uuid.NAMESPACE_URL,'webuddy:agent-pack:mfd-xml-conversion').hex
        planner=service.runtime_settings.get()['profiles'].get('planner',{})
        def chat_provider(a):
            try: ver=agents.version(a['id'])
            except Exception: return planner.get('provider')
            settings=(ver.get('model_settings') or {}).get('default')
            if not isinstance(settings,dict) or not settings.get('model'): settings=planner
            return settings.get('provider')
        annotated=[{**a,'chat_capable':chat_provider(a)=='claude'} for a in agents.list()]
        return routing.classify(body.text,annotated,explicit_agent_id=body.agent_id,
            actor=actor(request),mfd_agent_id=mfd_id,mfd_executable=False,
            attachments=[att.model_dump() for att in body.attachments])
    @api.post('/agents',status_code=201)
    def create(body:AgentCreate,request:Request):
        data=body.model_dump(); data={k:v for k,v in data.items() if v is not None}
        identity=data.pop('identity',None)
        result=guarded(agents.create,data,str(actor(request)['id']))
        if identity is not None:
            m=manifests.get(result['id'])
            guarded(manifests.save,result['id'],{'identity':identity,'skills':[],'assertions':data.get('acceptance',[])},m['revision'],actor(request)['id'])
        return result
    @api.get('/agents/{aid}')
    def get(aid:str):
        a=guarded(agents.get,aid); return {**a,'version':agents.version(aid),'versions':agents.versions(aid),'draft':agents.draft(aid)}
    @api.get('/agents/{aid}/versions')
    def versions(aid:str): return {'versions':guarded(agents.versions,aid)}
    @api.post('/agents/{aid}/conversations',status_code=201)
    def conversation(aid:str,body:ConversationCreate,request:Request):
        return guarded(agents.create_conversation,aid,body.mode,body.project_id,actor(request)['id'],body.client_key)
    @api.get('/agents/{aid}/conversations')
    def conversations(aid:str,request:Request):
        rows=guarded(agents.conversations,aid); uid=actor(request)['id']
        return {'conversations':[c for c in rows if c.get('actor_id')==uid or actor(request).get('role')=='admin']}
    @api.get('/conversations/{cid}')
    def read_conversation(cid:str,request:Request):
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        return c
    @api.post('/conversations/{cid}/attachments',status_code=201)
    def add_attachment(cid:str,request:Request,file:UploadFile=File(...)):
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        raw=file.file.read(60_001)
        if len(raw)>60_000: raise HTTPException(413,'附件过大，请上传 40 KB 以内的文本')
        try: text=raw.decode('utf-8')
        except UnicodeDecodeError: raise HTTPException(422,'只支持 UTF-8 文本附件（.txt/.md/.csv）') from None
        att=guarded(agents.add_attachment,cid,actor(request)['id'],file.filename,text)
        return {'attachment':att,'conversation':agents.conversation(cid)}
    @api.post('/conversations/{cid}/calc')
    def conversation_calc(cid:str,body:QuoteCalc,request:Request):
        # Deterministic arithmetic: a verifiable total from explicit inputs; no model,
        # no project, no coding run. The inputs are the source of truth.
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        from factory.control.quote_calc import quote
        try: return quote([i.model_dump() for i in body.items], body.discount_rate)
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @api.post('/conversations/{cid}/export',status_code=201)
    def export_document(cid:str,body:ExportDoc,request:Request):
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        item=guarded(agents.add_export,cid,actor(request)['id'],body.title,body.format,body.content)
        return {'export':item,'conversation':agents.conversation(cid)}

    @api.get('/conversations/{cid}/exports/{eid}/download')
    def download_export(cid:str,eid:str,request:Request):
        from fastapi.responses import Response
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        item=guarded(agents.export_document,cid,eid,actor(request)['id'])
        media={'md':'text/markdown','txt':'text/plain','csv':'text/csv'}[item['format']]
        return Response(item['content'].encode('utf-8'),media_type=media+'; charset=utf-8',headers={
            'Content-Disposition':f'attachment; filename="export-{eid}.{item["format"]}"','X-Content-Type-Options':'nosniff'})

    @api.post('/conversations/{cid}/messages',status_code=201)
    def message(cid:str,body:Message,request:Request):
        with service.lock:
            return process_message(cid, body, request)

    @api.post('/conversations/{cid}/retry', status_code=201)
    def retry_maintenance(cid: str, request: Request):
        c = guarded(agents.conversation, cid)
        if c.get('actor_id') != actor(request)['id'] and actor(request).get('role') != 'admin':
            raise HTTPException(403, '无权访问该会话')
        if c['mode'] != 'maintain':
            raise Conflict('只有维护对话可以重新整理')
        jobs = [m for m in c['messages'] if m.get('job_id')]
        if not jobs or guarded(service.maintenance_status, jobs[-1]['job_id'])['status'] not in ('failed', 'cancelled', 'interrupted'):
            raise Conflict('当前没有需要重试的维护任务')
        content = next((m['content'] for m in reversed(c['messages']) if m['role'] == 'user'), None)
        if not content:
            raise Conflict('请先提交需要整理的资料')
        return process_message(cid, Message(content=content), request, retry=True)

    def _chat_active_job(c):
        """The still-running answer job for a no-project chat, if any."""
        for m in reversed(c['messages']):
            if m.get('role')=='assistant' and m.get('job_id') and m.get('status') in ('pending','running','cancel_requested'):
                try: st=service.maintenance_status(m['job_id'])
                except KeyError: st={'status':'interrupted'}
                if st.get('status') in ('pending','running','cancel_requested'):
                    return m['job_id'], st['status']
                return None
        return None

    def process_message(cid, body, request, retry=False):
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        key=getattr(body,'idempotency_key',None)
        import hashlib as _hashlib
        content_sha=_hashlib.sha256(body.content.encode()).hexdigest()
        # No-project daily chat: distinguish a duplicate submit from a genuinely new
        # message BEFORE persisting anything.
        if not retry and c['mode']=='do' and not c.get('project_id'):
            if key:
                for m in c['messages']:
                    if m.get('role')=='user' and m.get('client_key')==key:
                        # Same key, different content is a mistake, not a replay: reject clearly.
                        if m.get('content_sha') and m['content_sha']!=content_sha:
                            raise HTTPException(409,'这个提交标识已用于不同内容，请刷新或改用新标识。')
                        # Replay the ORIGINAL message's real job state (never fake completed,
                        # never another message's run).
                        jid=m.get('answer_job')
                        try: st=service.maintenance_status(jid)['status'] if jid else 'completed'
                        except KeyError: st='interrupted'
                        return {'conversation':agents.conversation(cid),'run':None,'job_id':jid,'status':st,'idempotent_replay':True}
            active=_chat_active_job(c)
            if active:
                # A different message arrived while answering: reject clearly and keep the
                # draft on the client; do not persist it or pretend the old job handled it.
                return {'conversation':agents.conversation(cid),'run':None,'busy':True,
                        'job_id':active[0],'status':active[1],
                        'message':'上一条还在回答，请等它完成后再发送。'}
        prior_run = store.get(c['run_id']) if c.get('run_id') else None
        queued = (c['mode'] == 'do' and prior_run and prior_run['status'] in
                  ('received','planning','queued','running','verifying','publishing','ready_for_review','published'))
        if not retry:
            extra = {'feedback_status': 'pending'} if queued else {}
            if key: extra['client_key']=key
            if c['mode']=='do' and not c.get('project_id'): extra['content_sha']=content_sha
            agents.append_message(cid, 'user', body.content, **extra)
        c=agents.conversation(cid)
        if c['mode']=='do':
            if not c.get('project_id'):
                # A general question is answered by the real model first, reading only
                # this role's frozen skills and granted materials; an execution request
                # can then be associated with a project.
                version=agents.version(c['agent_id']); cfg=service.runtime_settings.get()
                # Freeze the capability version to this conversation on first use; reuse it
                # afterwards so a later role update never rewrites an existing chat.
                snapshot=agents.conversation_snapshot(cid)
                if not snapshot:
                    snapshot=manifests.freeze(c['agent_id'],version)
                    agents.freeze_conversation_snapshot(cid,snapshot,version['version'])
                settings=(snapshot.get('model_settings') or {}).get('default')
                if not isinstance(settings,dict) or not settings.get('model'): settings=cfg['profiles']['planner']
                if not settings.get('model'): return {'conversation':agents.conversation(cid),'run':None,'needs_project':True}
                # Bounded, read-only mount of this role's granted skills and reference
                # materials; ownership is enforced inside compile_mounts (no cross-role/user).
                from factory.control.mounts import compile_mounts
                try:
                    mount=compile_mounts(store,{'agent_snapshot':snapshot,'agent_id':c['agent_id'],'project_id':None,'module_snapshot':[],'context':{},'conversation_attachments':agents.conversation_attachments(cid)})
                except (ValueError, PermissionError) as exc:
                    job_id=uuid.uuid4().hex
                    agents.append_message(cid,'assistant','能力资料装载失败，未作答：'+str(exc),status='failed',job_id=job_id)
                    return {'conversation':agents.conversation(cid),'run':None,'job_id':job_id,'status':'failed'}
                reference=mount if mount.get("documents") else None
                job_id=uuid.uuid4().hex
                catalog_note='' if reference is None else '\nYou have read-only reference tools exposing this role\'s granted skills and materials. Read them before answering and cite the source id. Do not invent facts not present in the materials or the user\'s message.'
                prompt='Answer the user briefly as a standalone role assistant. Ordinary questions, quotations, meeting summaries, and downloadable conversation documents do not require a project. Do not append project-association advice to those answers. Only if the user explicitly requests repository edits or execution of development checks, explain that these require an associated project. If tax treatment, currency, or other terms are absent, mark them as unspecified; do not infer that a quote is tax-inclusive or tax-exclusive.'+catalog_note+'\nAGENT:\n'+snapshot.get('instructions','')+'\nHISTORY:\n'+json.dumps([{'role':m.get('role'),'content':m.get('content')} for m in c['messages']],ensure_ascii=False)
                # Chat tools (calc/export) bound to THIS conversation and user; the model
                # cannot target another conversation. Exposed as mcp__session__*.
                from factory.control import conversation_tools as _ct
                conversation_binding=_ct.binding_for(store,cid,actor(request)['id'],actor_role=actor(request).get('role'))
                prompt+='\nYou may use mcp__session__calc for any money arithmetic (decimal strings) and mcp__session__export to save a downloadable md/txt/csv document for the user; both act only on this conversation.'
                from factory.control.providers import ProviderRequest
                from factory.control.governance import GovernedRunner
                def answer(cancel):
                    with tempfile.TemporaryDirectory(prefix='factory-agent-chat-') as workspace:
                        runner=GovernedRunner(service.runner,service.governance,run_id=None,actor_id=actor(request)['id']) if service.governance else service.runner
                        result=runner.run(ProviderRequest(provider=settings['provider'],model=settings['model'],prompt=prompt,workspace=workspace,timeout_s=cfg['limits']['timeout_s'],read_only=True,reference_mount=reference,conversation_binding=conversation_binding),lambda *_:None,cancel)
                    agents.append_message(cid,'assistant',result.text,status='completed',job_id=job_id,usage={'cost_usd':getattr(result,'cost_usd',None),'tokens_in':getattr(result,'tokens_in',None),'tokens_out':getattr(result,'tokens_out',None)})
                    return {'status':'completed'}
                answer.on_error=lambda exc: agents.append_message(cid,'assistant','回答失败：'+str(exc),status='failed',job_id=job_id)
                job=service.start_maintenance(answer,job_id=job_id,conversation_id=cid,actor_id=actor(request)['id'])
                agents.link_answer_job(cid,job_id,key)  # bind this job to the message it answers
                _append_pending(cid,'正在回答',job_id)
                return {'conversation':agents.conversation(cid),'run':None,'job_id':job['id'],'status':'pending'}
            # A conversation has at most one active run.
            with service.lock:
                c = agents.conversation(cid)
                if c.get('run_id'):
                    prior=store.get(c['run_id'])
                else:
                    prior=None
                if prior and prior.get('status') in ('awaiting_approval','needs_clarification','needs_human'):
                    pending_messages = []
                    pending_chars = 0
                    for m in c['messages']:
                        if m.get('feedback_status') != 'pending':
                            continue
                        if pending_chars + len(m['content']) + len(body.content) + 100 > 90_000:
                            break
                        pending_messages.append(m)
                        pending_chars += len(m['content'])
                    pending_ids = [m['id'] for m in pending_messages]
                    answer = '\n\n'.join([*(m['content'] for m in pending_messages), body.content])
                    continuing = prior['status'] == 'needs_human' and prior.get('plan') and (prior.get('artifacts') or {}).get('tasks')
                    resumed = guarded(service.continue_run, prior['id'], body.content, prior['revision'], prior.get('resume_count', 0), actor(request)['username']) if continuing else guarded(service.clarify, prior['id'], answer, actor(request)['username'], feedback_message_ids=pending_ids)
                    if not continuing:
                        agents.settle_feedback(cid, prior['id'], pending_ids)
                    agents.append_message(cid,'assistant','已收到回答，正在按原计划继续未完成任务。' if continuing else '已将补充信息交给原任务，正在重新规划。',status='completed')
                    return {'conversation':agents.conversation(cid),'run':resumed}
                if prior and prior.get('status') in ('received','planning','queued','running','verifying','publishing','ready_for_review','published'):
                    agents.append_message(cid,'assistant','补充需求已排队，当前任务完成后会自动接续；如遇需人工判断的问题，会保留补充并提示处理。',status='completed')
                    service._ensure_scheduler()
                    service.wake.set()
                    return {'conversation':agents.conversation(cid),'run':prior,'waiting_for_active_run':True,'feedback_queued':True}
                version=agents.version(c['agent_id'])
                conversation_request='\n\n'.join(str(m.get('content','')) for m in c['messages'] if m.get('role')=='user')
                run,_=store.create_run(c['project_id'],conversation_request,source={'type':'agent','actor_id':actor(request)['id'],'agent_id':c['agent_id'],'agent_version':version['version'],'conversation_id':cid})
                frozen={**manifests.freeze(c['agent_id'],version),'frozen_at':version.get('created_at') or ''}
                runtime=service.runtime_settings.get(); overrides=version.get('model_settings') or {}
                default = overrides.get('default')
                def configured(*candidates):
                    return next((v for v in candidates if isinstance(v, dict) and v.get('provider') and v.get('model', '').strip()), None)
                planning = configured(overrides.get('planning'), overrides.get('analysis'), default)
                execution = configured(overrides.get('execution'), default)
                if planning:
                    runtime['profiles']['planner'] = {k: planning[k] for k in ('provider', 'model')}
                if execution:
                    for role in ('cheap', 'standard', 'strong'):
                        runtime['profiles'][role] = {k: execution[k] for k in ('provider', 'model')}
                verification=configured(overrides.get('verification'), default)
                if isinstance(verification,dict) and verification.get('provider') and verification.get('model'):
                    runtime['agent_verification_profile']=verification
                store.update(run['id'],{'agent_id':c['agent_id'],'agent_version':version['version'],'agent_snapshot':frozen,'conversation_id':cid,'runtime_configuration':runtime},expected=('received',),event=('agent.version_frozen',{'agent_id':c['agent_id'],'version':version['version'],'configuration_revision':runtime['revision']}))
                agents.attach_run(cid,run['id'])
                agents.settle_feedback(cid,run['id'])
            service.start_plan(run['id']); return {'conversation':agents.conversation(cid),'run':store.get(run['id'])}
        # Maintenance calls the configured real provider and persists only a structured draft.
        for item in reversed(c['messages'][:-1]):
            if item.get('status') == 'pending':
                try: job_state=service.maintenance_status(item.get('job_id'))
                except KeyError: job_state={'status':'interrupted'}
                if job_state.get('status') not in ('completed','failed','cancelled','interrupted'):
                    return {'conversation': c, 'job_id': item.get('job_id'), 'status': job_state.get('status','pending')}
        cfg=service.runtime_settings.get(); settings=(agents.version(c['agent_id']).get('model_settings') or {}).get('maintenance')
        if not isinstance(settings,dict) or not settings.get('model'):
            settings=(agents.version(c['agent_id']).get('model_settings') or {}).get('default')
        if not isinstance(settings,dict) or not settings.get('model'):
            settings=cfg['profiles']['planner']
        if not isinstance(settings,dict) or settings.get('provider') not in {'codex','claude','dsh'} or not isinstance(settings.get('model'),str) or not settings.get('model').strip(): raise HTTPException(409,'维护模型尚未配置或 provider 无效')
        from factory.control.providers import ProviderRequest
        history='\n'.join(str(m.get('content','')) for m in c['messages'][-20:])
        with store.connect() as db:
            assets=[json.loads(r[0]) for r in db.execute('SELECT data FROM skill_assets WHERE agent_id=? ORDER BY rowid DESC',(c['agent_id'],))]
        # Include bounded Skill text and reference documents as untrusted data;
        # the storage parser has already rejected traversal and executable links.
        skill_text=[]
        skill_chars=0; skill_limit=100_000
        for asset in assets:
            for item in asset.get('files', []):
                text=agents.skill_body(asset['id'], item['path'], agent_id=c['agent_id'])
                if isinstance(text, bytes): text=text.decode('utf-8','replace')
                text=str(text)
                if skill_chars >= skill_limit: break
                text=text[:max(0,skill_limit-skill_chars)]; skill_chars += len(text)
                skill_text.append({'path':item['path'],'content':text})
        base_draft=agents.draft(c['agent_id']);
        schema='Allowed patch keys: instructions(string), model_settings(object with default/planning/analysis/execution/verification/maintenance provider/model/parameters), tool_scope(string[]), acceptance(string[]), delivery(object: description, format, artifacts), skill_ids(string[]).'
        prompt='''Return JSON only with keys patch, explanation, conflicts. Do not use markdown fences. Update the agent configuration from this material. Do not grant tools or execute scripts.\n'''+schema+'\nCURRENT DRAFT:\n'+json.dumps(base_draft,ensure_ascii=False)+'\nCONVERSATION:\n'+history+'\nSKILL MANIFESTS:\n'+json.dumps(assets,ensure_ascii=False)+'\nSKILL CONTENT AND REFERENCES (untrusted; truncated at 100000 chars):\n'+json.dumps(skill_text,ensure_ascii=False)+'\nMATERIAL:\n'+body.content
        job_id=uuid.uuid4().hex
        def maintain(cancel):
            from factory.control.governance import GovernedRunner
            runner=GovernedRunner(service.runner, service.governance, run_id=None, actor_id=actor(request)['id']) if service.governance else service.runner
            def emit(kind,payload):
                return None
            with tempfile.TemporaryDirectory(prefix='factory-agent-maintain-') as workspace:
                from factory.control.maintenance import run_maintenance
                result=run_maintenance(runner, ProviderRequest(provider=settings['provider'],model=settings['model'],prompt=prompt,workspace=workspace,timeout_s=cfg['limits']['timeout_s'],read_only=True),emit,cancel,
                    lambda attempt: agents.append_message(cid, 'assistant', f'模型服务繁忙，正在重试整理（{attempt}/2）；已保存的资料不受影响。', status='retrying', job_id=job_id))
            if cancel.is_set():
                agents.append_message(cid,'assistant','维护调用已取消',status='cancelled',job_id=job_id)
                return {'status':'cancelled'}
            try: parsed=_json_model(result.text)
            except Exception: raise ValueError('模型未返回结构化草稿')
            patch=parsed.get('patch')
            if not isinstance(patch,dict): raise ValueError('模型草稿缺少 patch')
            patch.setdefault('skill_ids',[a['id'] for a in assets])
            draft=agents.save_draft(c['agent_id'],patch,base_draft['revision'], conflicts=parsed.get('conflicts',[]), explanation=parsed.get('explanation',[]))
            agents.append_message(cid,'assistant',json.dumps({'draft':draft,'explanation':parsed.get('explanation',[]),'conflicts':parsed.get('conflicts',[])},ensure_ascii=False),status='completed',job_id=job_id,usage={'cost_usd':getattr(result,'cost_usd',None),'tokens_in':getattr(result,'tokens_in',None),'tokens_out':getattr(result,'tokens_out',None)})
            return {'status':'completed','draft':draft}
        from factory.control.maintenance import failure_message
        maintain.on_error=lambda exc: agents.append_message(cid,'assistant',failure_message(exc),status='failed',job_id=job_id)
        job=service.start_maintenance(maintain, job_id=job_id, conversation_id=cid, actor_id=actor(request)['id'])
        _append_pending(cid, '维护整理已排队', job_id)
        return {'conversation':agents.conversation(cid),'job_id':job['id'],'status':'pending'}
    @api.post('/conversations/{cid}/project')
    def bind_project(cid:str, body:dict, request:Request):
        with service.lock:
            c=guarded(agents.conversation,cid)
            if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
            if c.get('run_id'):
                raise HTTPException(409,'会话已有运行，不能切换项目')
            pid=body.get('project_id')
            if not isinstance(pid,str) or not pid: raise HTTPException(422,'请选择项目')
            guarded(store.project,pid)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                c=json.loads(db.execute('SELECT data FROM agent_conversations WHERE id=?',(cid,)).fetchone()['data'])
                c['project_id']=pid
                db.execute('UPDATE agent_conversations SET data=? WHERE id=?',(json.dumps(c,ensure_ascii=False),cid))
            return c
    @api.get('/agents/{aid}/draft')
    def draft(aid:str): return guarded(agents.draft,aid)
    @api.patch('/agents/{aid}/draft')
    def patch_draft(aid:str,body:DraftPatch): return guarded(agents.save_draft,aid,body.patch,body.expected_revision)
    @api.post('/agents/{aid}/draft/apply')
    def apply(aid:str,body:Apply,request:Request):
        d=guarded(agents.draft,aid)
        if d.get('conflicts'): raise HTTPException(409,'草稿存在未决冲突，请先解决后再应用')
        return guarded(agents.apply,aid,body.expected_revision,body.idempotency_key,actor=str(actor(request)['id']))
    @api.post('/agents/{aid}/rollback')
    def rollback(aid:str,body:Rollback): return guarded(agents.rollback,aid,body.version)
    @api.get('/maintenance-jobs/{job_id}')
    def maintenance_job(job_id:str,request:Request):
        job=guarded(service.maintenance_status,job_id)
        if job.get('actor_id') not in (0, actor(request)['id']) and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该维护任务')
        return job
    @api.post('/maintenance-jobs/{job_id}/cancel')
    def cancel_maintenance(job_id:str,request:Request): return guarded(service.cancel_maintenance,job_id,str(actor(request)['id']))
    @api.post('/agents/{aid}/skills',status_code=201)
    def skill(aid:str,request:Request,file:UploadFile=File(...)):
        guarded(agents.get, aid)
        raw=file.file.read(MAX_ZIP+1)
        guidance = '请改用职能体页的“外部 skill 包·适配与人签”通道'
        try:
            meta = inspect_skill(raw, max_files=MAX_FILES)
        except ValueError as exc:
            message = str(exc)
            if '文件数超过' in message:
                message += '；' + guidance
            raise HTTPException(400, message) from None
        if sum(f['path'].replace('\\', '/').rsplit('/', 1)[-1].casefold() == 'skill.md' for f in meta['files']) > 1:
            raise HTTPException(400, '单附件包含多个 SKILL.md；' + guidance)
        raw = strip_macos_junk(raw)
        sid=__import__('uuid').uuid4().hex
        data={**meta,'id':sid,'agent_id':aid,'filename':file.filename or 'skill.zip','source':'upload','created_at':__import__('factory.control.store',fromlist=['now']).now()}
        with store.connect() as db: db.execute('INSERT INTO skill_assets VALUES (?,?,?,?,?)',(sid,aid,json.dumps(data,ensure_ascii=False),raw,data['created_at']))
        return data
    @api.get('/agents/{aid}/skills')
    def skills(aid:str):
        agents.get(aid)
        with store.connect() as db:
            return {'skills':[json.loads(r[0]) for r in db.execute('SELECT data FROM skill_assets WHERE agent_id=? ORDER BY rowid DESC',(aid,))]}
    return api

from factory.control.agents import MAX_ZIP
