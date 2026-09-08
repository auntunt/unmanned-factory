"""Authenticated runtime settings and explicit provider probe routes."""
from __future__ import annotations

import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from factory.control.providers import ProviderRequest
from factory.control.runtime import PROVIDERS, ROLES, RuntimeSettings, inspect_runtime
from factory.control.store import Conflict


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeProfilesBody(StrictBody):
    revision: int = Field(ge=1, strict=True)
    profiles: dict
    limits: dict


class RuntimeProbeBody(StrictBody):
    profile: str = Field(min_length=1, max_length=20)
    configuration_revision: int = Field(ge=1, strict=True)


_probe_gate = threading.Lock()


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def router(store, service, workspace_root=None, static_dir=None):
    """Build the runtime router using the service-owned RuntimeSettings."""
    settings = getattr(service, "runtime_settings", None)
    if not isinstance(settings, RuntimeSettings):
        raise ValueError("service.runtime_settings is required")
    api = APIRouter(prefix="/api/v2")

    @api.get("/runtime")
    def runtime():
        return inspect_runtime(settings, workspace_root, static_dir)

    @api.put("/runtime/profiles")
    def update_runtime(body: RuntimeProfilesBody, request: Request):
        try:
            return settings.update({"profiles": body.profiles, "limits": body.limits},
                                   body.revision, request.state.user["username"])
        except Conflict:
            raise HTTPException(409, "运行配置已更新，请重新加载后再保存") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @api.post("/runtime/probe")
    def probe(body: RuntimeProbeBody):
        if body.profile not in ROLES:
            raise HTTPException(400, "未知运行档位")
        if not _probe_gate.acquire(blocking=False):
            raise Conflict("已有运行探针正在执行，请稍后重试")
        try:
            config = settings.get()
            if body.configuration_revision != config["revision"]:
                raise HTTPException(409, "运行配置已更新，请重新加载后再探测")
            profile = config["profiles"][body.profile]
            checked_at = _checked_at()
            result = {
                "id": uuid.uuid4().hex,
                "profile": body.profile,
                "provider": profile["provider"],
                "model": profile["model"],
                "configuration_revision": config["revision"],
                "checked_at": checked_at,
                "outcome": "failed",
                "message": "",
            }
            if profile["provider"] == "dsh":
                result.update(outcome="unsupported", message="DeepSeek Harness 没有已验证的只读能力")
            else:
                from factory.control.runtime import profile_blockers
                # Real SDKRunner gets deterministic package/capability checks.
                # Tests and explicit injected runners are allowed to stand in
                # for that boundary without requiring local provider wheels.
                if service.runner.__class__.__module__ == "factory.control.providers":
                    blockers = profile_blockers(profile, body.profile)
                else:
                    blockers = [] if profile.get("model") else [f"profile {body.profile} model is not configured"]
                if blockers:
                    result["message"] = "; ".join(blockers)
                else:
                    try:
                        with tempfile.TemporaryDirectory(prefix="factory-runtime-probe-") as workspace:
                            request = ProviderRequest(
                                provider=profile["provider"], model=profile["model"],
                                prompt="Reply with exactly: OK",
                                workspace=workspace, timeout_s=30, read_only=True,
                            )
                            response = service.runner.run(request, lambda *_: None, threading.Event())
                            if response.text.strip() != "OK":
                                raise ValueError("Unexpected probe response")
                        result.update(outcome="passed", message="provider read-only probe completed")
                    except Exception as exc:
                        # Do not persist provider response bodies or credential
                        # material. The exception class is enough to triage.
                        result["message"] = f"provider probe failed ({type(exc).__name__})"
            return settings.record_probe(result)
        finally:
            _probe_gate.release()

    return api


__all__ = ["router"]
