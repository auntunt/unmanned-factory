"""Admin-written manifest composition and portable packs; members may read."""
from fastapi import APIRouter, HTTPException, Request, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from factory.control.store import Conflict
from factory.control.agents import MAX_ZIP
from factory.control.manifest_packs import export_pack, import_pack

class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')
class ManifestBody(Body):
    revision:int=Field(ge=1,strict=True)
    identity:str=Field(max_length=1200)
    skills:list[dict]=Field(max_length=24)
    assertions:list[str]=Field(max_length=100)
class Restore(Body):
    revision:int=Field(ge=1,strict=True)
    target_revision:int=Field(ge=1,strict=True)


def router(service):
    api=APIRouter(prefix='/api/v4')
    manifests=service.agent_manifests
    def guarded(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except Conflict:raise
        except KeyError:raise HTTPException(404,'清单或能力版本不存在') from None
        except (ValueError,UnicodeError) as e:raise HTTPException(400,str(e)) from None

    @api.get('/agents/{aid}/manifest')
    def get_manifest(aid:str):
        m=guarded(manifests.get,aid)
        return {**m,'resolved_skills':guarded(manifests.resolve,m),'history':guarded(manifests.history,aid)}

    @api.put('/agents/{aid}/manifest')
    def save_manifest(aid:str,body:ManifestBody,request:Request):
        return guarded(manifests.save,aid,body.model_dump(exclude={'revision'}),body.revision,request.state.user['id'])

    @api.post('/agents/{aid}/manifest/restore')
    def restore_manifest(aid:str,body:Restore,request:Request):
        return guarded(manifests.rollback,aid,body.target_revision,body.revision,request.state.user['id'])

    @api.get('/agents/{aid}/pack')
    def download_pack(aid:str):
        raw=guarded(export_pack,manifests,service.agents,aid)
        return Response(raw,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="agent-{aid}.zip"'})

    @api.post('/agent-packs/import',status_code=201)
    def upload_pack(request:Request,file:UploadFile=File(...)):
        return guarded(import_pack,manifests,file.file.read(MAX_ZIP+1),request.state.user['id'])
    return api
