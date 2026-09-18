"""Admin-only configuration tools for the conversation MCP session server.

These tools let an admin configure runtime profiles, operations automation,
and deploy targets through the conversation interface.  Each mutating tool
internally calls the **same** backend method that the form UI uses, so there
is exactly one source of truth.

Hard constraints (from N2 task spec):
  - No new HTTP configuration endpoints.
  - Admin identity must be verified before every mutation.
  - Tool inputs must NOT accept secret plaintext; the model receives only
    references / aliases / purpose strings.
  - Connection-check failures must return failure with specific gaps.
"""
from __future__ import annotations

import json
from typing import Any

from factory.redact import redact_text

# Tool names as they appear in the MCP session server (mcp__session__*).
ADMIN_TOOL_NAMES = frozenset({
    "mcp__session__get_runtime_config",
    "mcp__session__update_runtime_config",
    "mcp__session__get_operations_config",
    "mcp__session__configure_operations",
    "mcp__session__list_deploy_targets",
    "mcp__session__create_deploy_target",
    "mcp__session__update_deploy_target",
    "mcp__session__delete_deploy_target",
    "mcp__session__check_deploy_targets",
})


# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------

def _contains_secret(text: str) -> bool:
    """Return True if *text* contains a recognizable secret pattern."""
    return redact_text(text) != text


def _scan_args_for_secrets(args: dict[str, Any]) -> str | None:
    """Walk all string values in *args*; return the field path of the first
    secret found, or ``None`` if the input is clean."""

    def _walk(obj: Any, path: str = "") -> str | None:
        if isinstance(obj, str):
            if _contains_secret(obj):
                return path or "<root>"
        elif isinstance(obj, dict):
            for k, v in obj.items():
                result = _walk(v, f"{path}.{k}" if path else k)
                if result is not None:
                    return result
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                result = _walk(v, f"{path}[{i}]")
                if result is not None:
                    return result
        return None

    return _walk(args)


def _secret_rejection(field_path: str) -> dict[str, Any]:
    """Structured rejection when a tool input contains secret material."""
    return {
        "status": "rejected",
        "code": "secret_detected",
        "reason": f"输入字段 {field_path} 包含疑似密钥内容，请通过管理表单的密钥字段提供",
    }


# ---------------------------------------------------------------------------
# AdminConfigTools  (pure methods, testable without MCP server)
# ---------------------------------------------------------------------------

class AdminConfigTools:
    """Bound admin configuration tools.

    Every mutation method checks *actor_role* before proceeding and scans its
    arguments for secret patterns.  Read-only methods are available to any role.
    """

    def __init__(self, store: Any, actor_id: Any, actor_role: str) -> None:
        self.store = store
        self.actor_id = actor_id
        self.actor_role = actor_role

    def _require_admin(self) -> dict[str, Any] | None:
        if self.actor_role != "admin":
            return {
                "status": "rejected",
                "code": "admin_required",
                "reason": "此操作需要管理员权限",
            }
        return None

    def _actor_str(self) -> str:
        return str(self.actor_id)

    # --- Runtime --------------------------------------------------------

    def get_runtime_config(self) -> dict[str, Any]:
        from factory.control.runtime import RuntimeSettings
        settings = RuntimeSettings(self.store)
        return {"status": "ok", "config": settings.get()}

    def update_runtime_config(
        self,
        profiles: Any,
        limits: Any,
        expected_revision: int,
    ) -> dict[str, Any]:
        rejection = self._require_admin()
        if rejection:
            return rejection
        # Secret scan on the raw inputs
        secret_field = _scan_args_for_secrets({"profiles": profiles, "limits": limits})
        if secret_field:
            return _secret_rejection(secret_field)
        from factory.control.runtime import RuntimeSettings
        from factory.control.store import Conflict
        settings = RuntimeSettings(self.store)
        try:
            result = settings.update(
                {"profiles": profiles, "limits": limits},
                expected_revision,
                self._actor_str(),
            )
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        except Conflict as exc:
            return {"status": "conflict", "reason": str(exc)}
        return {"status": "ok", "config": result}

    # --- Operations Automation ------------------------------------------

    def get_operations_config(self) -> dict[str, Any]:
        from factory.control.operations_automation import OperationsAutomation
        ops = OperationsAutomation(self.store)
        return {"status": "ok", "config": ops.config()}

    def configure_operations(
        self,
        revision: int,
        knowledge_enabled: bool,
    ) -> dict[str, Any]:
        """Configure operations (knowledge toggle only).

        The webhook URL is a credential and must be set through the admin form,
        not through the conversation.
        """
        rejection = self._require_admin()
        if rejection:
            return rejection
        from factory.control.operations_automation import OperationsAutomation
        from factory.control.store import Conflict
        ops = OperationsAutomation(self.store)
        try:
            result = ops.configure(revision, knowledge_enabled)
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        except Conflict as exc:
            return {"status": "conflict", "reason": str(exc)}
        return {
            "status": "ok",
            "config": result,
            "credential_gaps": [
                {
                    "field": "webhook",
                    "message": "webhook URL 需要通过管理表单的密钥字段提供，不可通过对话配置",
                }
            ],
        }

    def configure_operations_with_check(
        self,
        revision: int,
        knowledge_enabled: bool,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Entry point that also rejects any secret in extra_args.

        The MCP tool schema does not expose a webhook parameter.  This method
        exists so that test code can verify that even if someone smuggles a
        webhook-shaped value through an ad-hoc argument, it is caught.
        """
        if extra_args:
            secret_field = _scan_args_for_secrets(extra_args)
            if secret_field:
                return _secret_rejection(secret_field)
            if "webhook" in extra_args:
                return {
                    "status": "rejected",
                    "code": "credential_gap",
                    "reason": "webhook URL 需要通过管理表单的密钥字段提供",
                }
        return self.configure_operations(revision, knowledge_enabled)

    # --- Deploy Targets -------------------------------------------------

    def list_deploy_targets(self) -> dict[str, Any]:
        from factory.control.deploy_targets import TargetStore
        targets = TargetStore(self.store)
        items = targets.list()
        checks = targets.last_checks()
        return {"status": "ok", "targets": items, "last_checks": checks}

    def create_deploy_target(self, values: dict[str, Any]) -> dict[str, Any]:
        rejection = self._require_admin()
        if rejection:
            return rejection
        secret_field = _scan_args_for_secrets(values)
        if secret_field:
            return _secret_rejection(secret_field)
        from factory.control.deploy_targets import TargetStore
        from factory.control.store import Conflict
        targets = TargetStore(self.store)
        try:
            result = targets.create(values, self._actor_str())
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        except Conflict as exc:
            return {"status": "conflict", "reason": str(exc)}
        return {"status": "ok", "target": result}

    def update_deploy_target(
        self,
        target_id: str,
        values: dict[str, Any],
        revision: int,
    ) -> dict[str, Any]:
        rejection = self._require_admin()
        if rejection:
            return rejection
        secret_field = _scan_args_for_secrets(values)
        if secret_field:
            return _secret_rejection(secret_field)
        from factory.control.deploy_targets import TargetStore
        from factory.control.store import Conflict
        targets = TargetStore(self.store)
        try:
            result = targets.update(target_id, values, revision, self._actor_str())
        except (ValueError, KeyError) as exc:
            return {"status": "error", "reason": str(exc)}
        except Conflict as exc:
            return {"status": "conflict", "reason": str(exc)}
        return {"status": "ok", "target": result}

    def delete_deploy_target(
        self,
        target_id: str,
        revision: int,
    ) -> dict[str, Any]:
        rejection = self._require_admin()
        if rejection:
            return rejection
        from factory.control.deploy_targets import TargetStore
        from factory.control.store import Conflict
        targets = TargetStore(self.store)
        try:
            targets.delete(target_id, revision, self._actor_str())
        except (ValueError, KeyError) as exc:
            return {"status": "error", "reason": str(exc)}
        except Conflict as exc:
            return {"status": "conflict", "reason": str(exc)}
        return {"status": "ok"}

    def check_deploy_targets(self) -> dict[str, Any]:
        from factory.control.deploy_targets import TargetStore
        targets = TargetStore(self.store)
        return {"status": "ok", "last_checks": targets.last_checks()}


# ---------------------------------------------------------------------------
# MCP tool registration (called from conversation_tools.create_server)
# ---------------------------------------------------------------------------

def register_admin_tools(admin_tools: AdminConfigTools, emit, *, tool_decorator):
    """Create and return a list of MCP tool definitions for admin configuration.

    *tool_decorator* is ``claude_agent_sdk.tool``; passed in to avoid importing
    the SDK at module level.
    """
    tools = []

    @tool_decorator(
        "get_runtime_config",
        "Return the current runtime configuration (profiles and limits) with its revision number.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def get_runtime_config(args):
        result = admin_tools.get_runtime_config()
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    tools.append(get_runtime_config)

    @tool_decorator(
        "update_runtime_config",
        "Update runtime profiles and limits.  Requires admin.  Pass profiles (planner/cheap/standard/strong, each with provider and model) and limits.  "
        "Model identifiers only; never pass API keys or secrets as model names.  "
        "Include expected_revision for optimistic locking.",
        {
            "type": "object",
            "properties": {
                "profiles": {
                    "type": "object",
                    "description": "Per-role provider and model.  Keys: planner, cheap, standard, strong.",
                },
                "limits": {
                    "type": "object",
                    "description": "timeout_s, max_parallel, max_tasks, unknown_cost_policy.",
                },
                "expected_revision": {"type": "integer"},
            },
            "required": ["profiles", "limits", "expected_revision"],
            "additionalProperties": False,
        },
    )
    async def update_runtime_config(args):
        result = admin_tools.update_runtime_config(
            args["profiles"], args["limits"], args["expected_revision"],
        )
        emit("session.admin_config", {"tool": "update_runtime_config", "status": result["status"]})
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            **({"is_error": True} if result["status"] != "ok" else {}),
        }

    tools.append(update_runtime_config)

    @tool_decorator(
        "get_operations_config",
        "Return the current operations automation configuration.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def get_operations_config(args):
        result = admin_tools.get_operations_config()
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    tools.append(get_operations_config)

    @tool_decorator(
        "configure_operations",
        "Update operations automation settings (knowledge_enabled toggle).  Requires admin.  "
        "The webhook URL must be configured through the admin form; it cannot be set here.",
        {
            "type": "object",
            "properties": {
                "revision": {"type": "integer"},
                "knowledge_enabled": {"type": "boolean"},
            },
            "required": ["revision", "knowledge_enabled"],
            "additionalProperties": False,
        },
    )
    async def configure_operations(args):
        result = admin_tools.configure_operations(args["revision"], args["knowledge_enabled"])
        emit("session.admin_config", {"tool": "configure_operations", "status": result["status"]})
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            **({"is_error": True} if result["status"] != "ok" else {}),
        }

    tools.append(configure_operations)

    @tool_decorator(
        "list_deploy_targets",
        "List all deploy targets and their last connection-check results.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def list_deploy_targets(args):
        result = admin_tools.list_deploy_targets()
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    tools.append(list_deploy_targets)

    @tool_decorator(
        "create_deploy_target",
        "Register a new deploy target (SSH).  Requires admin.  Provide name, host, port, user, host_fingerprint, "
        "and commands (health_check / service_status / fetch_log / deploy / rollback).  "
        "Commands must not contain credentials; configure them on the target server.  "
        "An Ed25519 key pair is generated automatically.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 120},
                "host": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "user": {"type": "string"},
                "host_fingerprint": {"type": "string", "description": "SHA256:<base64> fingerprint of the target host key."},
                "commands": {
                    "type": "object",
                    "description": "Pre-registered verbs: health_check, service_status, fetch_log, deploy, rollback.",
                },
            },
            "required": ["name", "host", "port", "user", "host_fingerprint", "commands"],
            "additionalProperties": False,
        },
    )
    async def create_deploy_target(args):
        result = admin_tools.create_deploy_target(args)
        emit("session.admin_config", {"tool": "create_deploy_target", "status": result["status"]})
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            **({"is_error": True} if result["status"] != "ok" else {}),
        }

    tools.append(create_deploy_target)

    @tool_decorator(
        "update_deploy_target",
        "Update an existing deploy target.  Requires admin.  Pass target_id, updated values, and revision for optimistic locking.",
        {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "values": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 120},
                        "host": {"type": "string"},
                        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                        "user": {"type": "string"},
                        "host_fingerprint": {"type": "string"},
                        "commands": {"type": "object"},
                    },
                    "required": ["name", "host", "port", "user", "host_fingerprint", "commands"],
                },
                "revision": {"type": "integer"},
            },
            "required": ["target_id", "values", "revision"],
            "additionalProperties": False,
        },
    )
    async def update_deploy_target(args):
        result = admin_tools.update_deploy_target(args["target_id"], args["values"], args["revision"])
        emit("session.admin_config", {"tool": "update_deploy_target", "status": result["status"]})
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            **({"is_error": True} if result["status"] != "ok" else {}),
        }

    tools.append(update_deploy_target)

    @tool_decorator(
        "delete_deploy_target",
        "Delete a deploy target.  Requires admin.  Pass target_id and revision.  "
        "Project bindings must be removed first.",
        {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "revision": {"type": "integer"},
            },
            "required": ["target_id", "revision"],
            "additionalProperties": False,
        },
    )
    async def delete_deploy_target(args):
        result = admin_tools.delete_deploy_target(args["target_id"], args["revision"])
        emit("session.admin_config", {"tool": "delete_deploy_target", "status": result["status"]})
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            **({"is_error": True} if result["status"] != "ok" else {}),
        }

    tools.append(delete_deploy_target)

    @tool_decorator(
        "check_deploy_targets",
        "Return the last connection-check result for each deploy target.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def check_deploy_targets(args):
        result = admin_tools.check_deploy_targets()
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

    tools.append(check_deploy_targets)

    return tools
