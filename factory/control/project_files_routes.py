"""Project creation from sample files; reuse bounded archive import and agent binding."""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from factory.control.project_import import import_files, ImportError
from factory.control.project_assistants import ProjectAssistants
from factory.control.workspaces import WorkspaceError


def router(service, root):
    api = APIRouter()

    @api.post('/api/v2/projects/import-files', status_code=201)
    def upload(request: Request, files: list[UploadFile] = File(...),
               name: str = Form(..., min_length=1, max_length=120),
               idempotency_key: str = Form(..., min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$'),
               agent_id: str | None = Form(None)):
        try:
            if not name.strip():
                raise HTTPException(422, '请填写项目名称')
            helpers = ProjectAssistants(service.store)
            if agent_id:
                try:
                    helpers.agents.get(agent_id)
                except KeyError:
                    raise HTTPException(404, '所选职能体不存在') from None
            result = import_files(service.store, root, files, name=name.strip(), budget_usd=None,
                actor_id=request.state.user['id'], idempotency_key=idempotency_key, agent_id=agent_id)
            if agent_id and helpers.binding(result['project']['id'])['revision'] == 0:
                helpers.bind(result['project']['id'], agent_id, 0, request.state.user['id'])
            return result
        except ImportError as exc:
            raise HTTPException(400, str(exc)) from None
        except WorkspaceError as exc:
            raise HTTPException(503, str(exc)) from None
        finally:
            for file in files:
                file.file.close()

    return api
