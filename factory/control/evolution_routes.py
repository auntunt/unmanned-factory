"""Evidence-only proposal APIs; auto source provenance is never caller supplied."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from factory.control.store import Conflict

class Body(BaseModel):model_config=ConfigDict(extra='forbid')
class Proposal(Body):
    project_id:str
    kind:str=Field(pattern='^(add_skill|remove_skill|upgrade_skill|add_assertion)$')
    change:dict
    evidence:dict
class Decision(Body):
    approve:bool=Field(strict=True)
    revision:int=Field(ge=1,strict=True)
class Policy(Body):
    revision:int=Field(ge=0,strict=True)
    auto_evolve:bool=Field(strict=True)
    notifications:bool=Field(strict=True)
class Promotion(Body):
    agent_id:str
    revision:int=Field(ge=1,strict=True)


def router(service):
    api=APIRouter(prefix='/api/v4');e=service.evolution
    def guarded(fn,*args):
        try:return fn(*args)
        except Conflict:raise
        except KeyError:raise HTTPException(404,'提案、职能体或来源不存在') from None
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
    def check(request,pid):
        if service.governance:service.governance.require_project(request.state.user['id'],pid)

    @api.get('/agents/{aid}/evolution')
    def proposals(aid:str,request:Request):
        rows=guarded(e.list,aid)
        if service.governance and request.state.user['role']!='admin':
            with service.governance.connect() as db:
                rows=[p for p in rows if service.governance._can_run(db,request.state.user['id'],p['project_id'])]
        return {'proposals':rows}

    @api.post('/agents/{aid}/evolution',status_code=201)
    def propose(aid:str,body:Proposal,request:Request):
        check(request,body.project_id)
        return guarded(e.create,aid,body.project_id,body.kind,body.change,body.evidence,request.state.user['id'])

    @api.post('/evolution/{proposal_id}/decision')
    def decide(proposal_id:str,body:Decision,request:Request):
        return guarded(e.decide,proposal_id,body.approve,body.revision,request.state.user['id'])

    @api.get('/projects/{pid}/evolution-policy')
    def policy(pid:str,request:Request):
        check(request,pid);return guarded(e.policy,pid)

    @api.put('/projects/{pid}/evolution-policy')
    def configure(pid:str,body:Policy,request:Request):
        check(request,pid)
        return guarded(e.configure,pid,body.revision,body.auto_evolve,body.notifications,request.state.user['id'])

    @api.post('/capabilities/{cid}/promote',status_code=201)
    def promote(cid:str,body:Promotion,request:Request):
        return guarded(e.promote,cid,body.revision,body.agent_id,request.state.user['id'])
    return api
