"""Authenticated v3 API for the versioned Agent capability library."""
from __future__ import annotations

import io
import json
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from factory.control.capabilities import CATEGORIES, CapabilityStore
from factory.control.store import Conflict


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class CapabilityBody(Body):
    name: StrictStr = Field(min_length=1, max_length=120)
    description: StrictStr = Field(max_length=4000)
    category: StrictStr
    instructions: StrictStr = Field(min_length=1, max_length=12000)
    input_description: StrictStr = Field(min_length=1, max_length=4000)
    output_description: StrictStr = Field(min_length=1, max_length=4000)
    acceptance: list[StrictStr] = Field(max_length=20)
    status: StrictStr = "draft"


class CapabilityUpdate(CapabilityBody):
    expected_revision: StrictInt = Field(ge=1)


class BindingBody(Body):
    capability_id: StrictStr = Field(min_length=1, max_length=200)
    revision: StrictInt = Field(ge=1)


class InvokeBody(Body):
    project_id: StrictStr = Field(min_length=1, max_length=200)
    revision: StrictInt = Field(ge=1)
    request: StrictStr = Field(min_length=5, max_length=50000)


class DistillBody(Body):
    name: StrictStr = Field(min_length=1, max_length=120)
    description: StrictStr | None = Field(default=None, max_length=4000)
    category: StrictStr | None = None


def router(store, service):
    api = APIRouter(prefix="/api/v3")
    capabilities = CapabilityStore(store)

    def guarded(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def actor(request: Request) -> str:
        user = getattr(request.state, "user", None)
        return str(user.get("username", "unknown")) if isinstance(user, dict) else "unknown"

    @api.get("/capabilities")
    def list_capabilities():
        return {"capabilities": capabilities.list()}

    @api.post("/capabilities", status_code=201)
    def create_capability(body: CapabilityBody, request: Request):
        return guarded(capabilities.create, body.model_dump(), actor=actor(request))

    @api.get("/capabilities/{capability_id}")
    def get_capability(capability_id: str):
        result = guarded(capabilities.get, capability_id)
        return {**result, "versions": guarded(capabilities.versions, capability_id)}

    @api.put("/capabilities/{capability_id}")
    def update_capability(capability_id: str, body: CapabilityUpdate, request: Request):
        return guarded(capabilities.update, capability_id,
                       body.model_dump(exclude={"expected_revision"}), body.expected_revision,
                       actor=actor(request))

    @api.get("/capabilities/{capability_id}/export")
    def export_capability(capability_id: str, revision: int | None = None, format: str = "zip"):
        capability = guarded(capabilities.get, capability_id, revision)
        manifest = {"schema_version": 1, "kind": "factory-capability",
                    "execution": "engineering-harness", "capability": capability}
        encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
        stem = f"factory-agent-{capability['id']}-v{capability['revision']}"
        if format == "json":
            return Response(encoded, media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{stem}.json"'})
        if format != "zip":
            raise HTTPException(400, "仅支持 zip 或 json")
        skill = ("---\nname: " + stem + "\ndescription: " +
                 json.dumps(capability['description'] or capability['name'], ensure_ascii=False) +
                 "\n---\n\n# " + capability['name'] + "\n\n" + capability['instructions'] +
                 "\n\n## 输入\n\n" + capability['input_description'] +
                 "\n\n## 输出\n\n" + capability['output_description'] +
                 "\n\n## 验收\n\n" + "\n".join("- " + line for line in capability['acceptance']) + "\n")
        readme = (f"# {capability['name']} · v{capability['revision']}\n\n"
                  f"状态：{capability['status']}。这是可迁移的能力定义与 SOP，不包含模型运行时、账户凭据或云资源。\n\n"
                  "将 agent.json 中的 capability 配置导入工厂能力库，并绑定到目标项目的明确版本；"
                  "配置模型、工作区和项目检查后调用。SKILL.md 可作为支持此格式的 Agent 的技能材料，"
                  "仍需目标运行时提供工具与权限。草稿需要补齐适用范围和验收条件后才可启用。\n\n"
                  "原项目执行成功只证明原需求的验收通过，不自动证明对其他项目适用。"
                  "更新能力会生成新版本，既有项目绑定不会随之漂移。\n")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("agent.json", encoded)
            archive.writestr("SKILL.md", skill)
            archive.writestr("README.md", readme)
            if capability.get('source_run_id'):
                source = store.get(capability['source_run_id'])
                archive.writestr("source-run.json", json.dumps({key: source.get(key)
                    for key in ('id', 'project_id', 'status', 'request', 'plan', 'artifacts')},
                    ensure_ascii=False, indent=2))
        return Response(output.getvalue(), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{stem}.zip"'})

    @api.get("/projects/{project_id}/capabilities")
    def project_capabilities(project_id: str):
        return {"bindings": guarded(capabilities.bindings, project_id)}

    @api.post("/projects/{project_id}/capabilities", status_code=201)
    def bind_capability(project_id: str, body: BindingBody, request: Request):
        return guarded(capabilities.bind, project_id, body.capability_id, body.revision,
                       actor=actor(request))

    @api.delete("/projects/{project_id}/capabilities/{capability_id}")
    def unbind_capability(project_id: str, capability_id: str, request: Request):
        return guarded(capabilities.unbind, project_id, capability_id, actor=actor(request))

    @api.post("/capabilities/{capability_id}/invoke")
    def invoke_capability(capability_id: str, body: InvokeBody, request: Request):
        snapshot = guarded(capabilities.invocation, body.project_id, capability_id, body.revision,
                           body.request, actor=actor(request))
        source = {**snapshot["source"], "actor_id": request.state.user['id']}
        run, _ = store.create_run(body.project_id, body.request, source=source)
        frozen = {key: value for key, value in snapshot.items() if key != "request"}
        run = store.update(run["id"], {"capability": frozen}, expected=("received",),
                           event=("capability.frozen", {
                               "capability_id": capability_id,
                               "capability_revision": body.revision,
                               "actor": actor(request),
                           }))
        try:
            service.start_plan(run["id"])
        except Exception as exc:
            if hasattr(service, "_fail"):
                service._fail(run["id"], exc)
            raise
        return store.get(run["id"])

    @api.post("/runs/{run_id}/distill", status_code=201)
    def distill_run(run_id: str, body: DistillBody, request: Request):
        run = store.get(run_id)
        data = body.model_dump()
        if data.get("description") is None:
            data.pop("description", None)
        if data.get("category") is None:
            data.pop("category", None)
        return guarded(capabilities.distill, run, data, actor=actor(request))

    return api
