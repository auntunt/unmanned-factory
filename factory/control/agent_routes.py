"""Authenticated agent and two-mode conversation API."""
from __future__ import annotations
import json
import tempfile
import uuid
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, ConfigDict, Field
from factory.control.agents import AgentStore, inspect_skill
from factory.control.store import Conflict

class Body(BaseModel): model_config=ConfigDict(extra='forbid')
class AgentCreate(Body):
    name:str=Field(min_length=1,max_length=120); purpose:str=Field(default='',max_length=4000)
    instructions:str=Field(default='',max_length=30000); model_settings:dict|None=None
    tool_scope:list[str]=Field(default_factory=list); acceptance:list[str]=Field(default_factory=list); delivery:dict|None=None
class ConversationCreate(Body): mode:str=Field(pattern='^(do|maintain)$'); project_id:str|None=None
class Message(Body): content:str=Field(min_length=1,max_length=50000)
class DraftPatch(Body): expected_revision:int=Field(ge=0); patch:dict
class Apply(Body): expected_revision:int=Field(ge=0); idempotency_key:str|None=None
class Rollback(Body): version:int=Field(ge=1)

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

    @api.get('/agents')
    def list_agents(): return {'agents':agents.list()}
    @api.post('/agents',status_code=201)
    def create(body:AgentCreate,request:Request):
        data=body.model_dump(); data={k:v for k,v in data.items() if v is not None}
        return guarded(agents.create,data,str(actor(request)['id']))
    @api.get('/agents/{aid}')
    def get(aid:str):
        a=guarded(agents.get,aid); return {**a,'version':agents.version(aid),'versions':agents.versions(aid),'draft':agents.draft(aid)}
    @api.get('/agents/{aid}/versions')
    def versions(aid:str): return {'versions':guarded(agents.versions,aid)}
    @api.post('/agents/{aid}/conversations',status_code=201)
    def conversation(aid:str,body:ConversationCreate,request:Request):
        return guarded(agents.create_conversation,aid,body.mode,body.project_id,actor(request)['id'])
    @api.get('/agents/{aid}/conversations')
    def conversations(aid:str,request:Request):
        rows=guarded(agents.conversations,aid); uid=actor(request)['id']
        return {'conversations':[c for c in rows if c.get('actor_id')==uid or actor(request).get('role')=='admin']}
    @api.get('/conversations/{cid}')
    def read_conversation(cid:str,request:Request):
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        return c
    @api.post('/conversations/{cid}/messages',status_code=201)
    def message(cid:str,body:Message,request:Request):
        c=guarded(agents.conversation,cid)
        if c.get('actor_id')!=actor(request)['id'] and actor(request).get('role')!='admin': raise HTTPException(403,'无权访问该会话')
        agents.append_message(cid,'user',body.content)
        c=agents.conversation(cid)
        if c['mode']=='do':
            if not c.get('project_id'):
                # A general question is answered by the real model first; an
                # execution request can then be associated with a project.
                version=agents.version(c['agent_id']); cfg=service.runtime_settings.get()
                settings=(version.get('model_settings') or {}).get('default')
                if not isinstance(settings,dict) or not settings.get('model'): settings=cfg['profiles']['planner']
                if not settings.get('model'): return {'conversation':agents.conversation(cid),'run':None,'needs_project':True}
                job_id=uuid.uuid4().hex
                prompt='Answer the user briefly. If the request requires changing files or running checks, clearly ask them to associate a project.\nAGENT:\n'+version.get('instructions','')+'\nHISTORY:\n'+json.dumps([{'role':m.get('role'),'content':m.get('content')} for m in c['messages']],ensure_ascii=False)
                from factory.control.providers import ProviderRequest
                from factory.control.governance import GovernedRunner
                def answer(cancel):
                    with tempfile.TemporaryDirectory(prefix='factory-agent-chat-') as workspace:
                        runner=GovernedRunner(service.runner,service.governance,run_id=None,actor_id=actor(request)['id']) if service.governance else service.runner
                        result=runner.run(ProviderRequest(provider=settings['provider'],model=settings['model'],prompt=prompt,workspace=workspace,timeout_s=cfg['limits']['timeout_s'],read_only=True),lambda *_:None,cancel)
                    agents.append_message(cid,'assistant',result.text,status='completed',job_id=job_id,usage={'cost_usd':getattr(result,'cost_usd',None),'tokens_in':getattr(result,'tokens_in',None),'tokens_out':getattr(result,'tokens_out',None)})
                    return {'status':'completed'}
                answer.on_error=lambda exc: agents.append_message(cid,'assistant','回答失败：'+str(exc),status='failed',job_id=job_id)
                job=service.start_maintenance(answer,job_id=job_id,conversation_id=cid,actor_id=actor(request)['id'])
                _append_pending(cid,'正在回答；如需修改文件，请随后关联项目',job_id)
                return {'conversation':agents.conversation(cid),'run':None,'job_id':job['id'],'status':'pending'}
            # A conversation has at most one active run.
            with service.lock:
                c = agents.conversation(cid)
                if c.get('run_id'):
                    prior=store.get(c['run_id'])
                else:
                    prior=None
                if prior and prior.get('status') in ('awaiting_approval','needs_clarification','needs_human'):
                    resumed = guarded(service.clarify, prior['id'], body.content, actor(request)['username'])
                    agents.append_message(cid,'assistant','已将补充信息交给原任务，正在重新规划。',status='completed')
                    return {'conversation':agents.conversation(cid),'run':resumed}
                if prior and prior.get('status') in ('received','planning','queued','running','verifying','publishing'):
                    agents.append_message(cid,'assistant','补充信息已保存到对话，当前运行尚未采用。请等当前任务结束后发送“继续”，后续任务会使用这些补充。',status='completed')
                    return {'conversation':agents.conversation(cid),'run':prior,'waiting_for_active_run':True}
                version=agents.version(c['agent_id'])
                conversation_request='\n\n'.join(str(m.get('content','')) for m in c['messages'] if m.get('role')=='user')
                run,_=store.create_run(c['project_id'],conversation_request,source={'type':'agent','actor_id':actor(request)['id'],'agent_id':c['agent_id'],'agent_version':version['version'],'conversation_id':cid})
                frozen={**version,'frozen_at':version.get('created_at') or ''}
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
                result=runner.run(ProviderRequest(provider=settings['provider'],model=settings['model'],prompt=prompt,workspace=workspace,timeout_s=cfg['limits']['timeout_s'],read_only=True),emit,cancel)
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
        maintain.on_error=lambda exc: agents.append_message(cid,'assistant','维护失败：'+str(exc),status='failed',job_id=job_id)
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
    def apply(aid:str,body:Apply):
        d=guarded(agents.draft,aid)
        if d.get('conflicts'): raise HTTPException(409,'草稿存在未决冲突，请先解决后再应用')
        return guarded(agents.apply,aid,body.expected_revision,body.idempotency_key)
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
        raw=file.file.read(MAX_ZIP+1); meta=guarded(inspect_skill,raw); sid=__import__('uuid').uuid4().hex
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
