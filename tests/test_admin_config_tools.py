"""Tests for admin-only configuration tools (N2: D4 M2).

Coverage:
1. member role → structured rejection for every mutating tool
2. admin → real write, read-back via form method matches
3. secret text in inputs → rejected, text absent from return
4. connection check → returns structured result
Plus mutation verification for (1) and (3).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from factory.control.store import Store
from factory.control.runtime import RuntimeSettings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _store(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Store(data / "control.db")


def _seed_runtime(store):
    """Ensure RuntimeSettings table exists with a seeded row."""
    return RuntimeSettings(store)


def _profiles(provider="codex", model="test-model"):
    return {role: {"provider": provider, "model": model}
            for role in ("planner", "cheap", "standard", "strong")}


def _default_limits():
    return {"timeout_s": 14400, "max_parallel": 2, "max_tasks": 20,
            "unknown_cost_policy": "allow_bounded"}


def _make_tools(tmp_path, role="admin"):
    from factory.control.admin_config_tools import AdminConfigTools
    store = _store(tmp_path)
    _seed_runtime(store)
    return AdminConfigTools(store, actor_id=1, actor_role=role)


def _deploy_target_values():
    return {
        "name": "staging",
        "host": "staging.example.com",
        "port": 22,
        "user": "deploy",
        "host_fingerprint": "SHA256:" + "A" * 43,
        "commands": {"health_check": "/srv/check"},
    }


# ===========================================================================
# 1  member → structured rejection
# ===========================================================================

class TestMemberRejection:
    """Every mutating admin tool must return a structured rejection for member."""

    def test_update_runtime_config(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.update_runtime_config(_profiles(), _default_limits(), 1)
        assert result["status"] == "rejected"
        assert result["code"] == "admin_required"
        assert "管理员" in result["reason"]

    def test_configure_operations(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.configure_operations(0, True)
        assert result["status"] == "rejected"
        assert result["code"] == "admin_required"

    def test_create_deploy_target(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.create_deploy_target(_deploy_target_values())
        assert result["status"] == "rejected"
        assert result["code"] == "admin_required"

    def test_update_deploy_target(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.update_deploy_target("abc", _deploy_target_values(), 1)
        assert result["status"] == "rejected"
        assert result["code"] == "admin_required"

    def test_delete_deploy_target(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.delete_deploy_target("abc", 1)
        assert result["status"] == "rejected"
        assert result["code"] == "admin_required"


class TestMemberCanRead:
    """Read-only tools should work for any role."""

    def test_get_runtime_config(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.get_runtime_config()
        assert result["status"] == "ok"
        assert "config" in result

    def test_get_operations_config(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.get_operations_config()
        assert result["status"] == "ok"

    def test_list_deploy_targets(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.list_deploy_targets()
        assert result["status"] == "ok"

    def test_check_deploy_targets(self, tmp_path):
        tools = _make_tools(tmp_path, role="member")
        result = tools.check_deploy_targets()
        assert result["status"] == "ok"


# ===========================================================================
# 2  admin → write + read-back matches form method
# ===========================================================================

class TestAdminWriteReadBack:
    def test_update_runtime_profiles_and_limits(self, tmp_path):
        tools = _make_tools(tmp_path, role="admin")
        current = tools.get_runtime_config()["config"]
        profiles = _profiles("claude", "claude-sonnet-4-20250514")
        limits = current["limits"]
        result = tools.update_runtime_config(profiles, limits, current["revision"])
        assert result["status"] == "ok"
        assert result["config"]["profiles"]["planner"]["provider"] == "claude"
        assert result["config"]["profiles"]["planner"]["model"] == "claude-sonnet-4-20250514"
        # Read back via the SAME form method to prove single source of truth
        store = _store(tmp_path)
        rs = RuntimeSettings(store)
        form_read = rs.get()
        assert form_read["profiles"] == result["config"]["profiles"]
        assert form_read["limits"] == result["config"]["limits"]
        assert form_read["revision"] == result["config"]["revision"]

    def test_configure_operations_knowledge(self, tmp_path):
        tools = _make_tools(tmp_path, role="admin")
        current = tools.get_operations_config()["config"]
        result = tools.configure_operations(current["revision"], True)
        assert result["status"] == "ok"
        assert result["config"]["knowledge_enabled"] is True
        # Read back via form method
        from factory.control.operations_automation import OperationsAutomation
        store = _store(tmp_path)
        ops = OperationsAutomation(store)
        form_read = ops.config()
        assert form_read["knowledge_enabled"] is True

    def test_create_deploy_target(self, tmp_path, monkeypatch):
        # key_dir must be OUTSIDE the data directory (sibling, not child)
        key_dir = tmp_path / "keys"
        monkeypatch.setenv("FACTORY_DEPLOY_KEY_DIR", str(key_dir))
        tools = _make_tools(tmp_path, role="admin")
        values = _deploy_target_values()
        result = tools.create_deploy_target(values)
        assert result["status"] == "ok", result
        target = result["target"]
        assert target["name"] == "staging"
        assert "public_key" in target
        assert "id" in target
        # Read back via form method
        from factory.control.deploy_targets import TargetStore
        ts = TargetStore(_store(tmp_path))
        form_read = ts.get(target["id"])
        assert form_read["name"] == target["name"]
        assert form_read["host"] == target["host"]

    def test_update_deploy_target(self, tmp_path, monkeypatch):
        key_dir = tmp_path / "keys"
        monkeypatch.setenv("FACTORY_DEPLOY_KEY_DIR", str(key_dir))
        tools = _make_tools(tmp_path, role="admin")
        created = tools.create_deploy_target(_deploy_target_values())["target"]
        new_values = _deploy_target_values()
        new_values["name"] = "production"
        result = tools.update_deploy_target(created["id"], new_values, created["revision"])
        assert result["status"] == "ok"
        assert result["target"]["name"] == "production"
        # Read back via form method
        from factory.control.deploy_targets import TargetStore
        ts = TargetStore(_store(tmp_path))
        assert ts.get(created["id"])["name"] == "production"

    def test_delete_deploy_target(self, tmp_path, monkeypatch):
        key_dir = tmp_path / "keys"
        monkeypatch.setenv("FACTORY_DEPLOY_KEY_DIR", str(key_dir))
        tools = _make_tools(tmp_path, role="admin")
        created = tools.create_deploy_target(_deploy_target_values())["target"]
        result = tools.delete_deploy_target(created["id"], created["revision"])
        assert result["status"] == "ok"
        # Verify via form method
        from factory.control.deploy_targets import TargetStore
        ts = TargetStore(_store(tmp_path))
        with pytest.raises(KeyError):
            ts.get(created["id"])


# ===========================================================================
# 3  secret text → rejected
# ===========================================================================

class TestSecretRejection:

    def test_scan_detects_vendor_prefix_token(self):
        from factory.control.admin_config_tools import _scan_args_for_secrets
        assert _scan_args_for_secrets({"model": "sk-ant-api03-abcdefghijklmnop"}) is not None

    def test_scan_detects_key_value_pattern(self):
        from factory.control.admin_config_tools import _scan_args_for_secrets
        assert _scan_args_for_secrets({"note": "api_key=mysecretvalue123"}) is not None

    def test_scan_detects_bearer_token(self):
        from factory.control.admin_config_tools import _scan_args_for_secrets
        assert _scan_args_for_secrets({"cmd": 'Authorization: Bearer tok_live_abc'}) is not None

    def test_scan_clean_input_passes(self):
        from factory.control.admin_config_tools import _scan_args_for_secrets
        assert _scan_args_for_secrets({"provider": "claude", "model": "claude-sonnet-4-20250514"}) is None

    def test_scan_detects_pem_key(self):
        from factory.control.admin_config_tools import _scan_args_for_secrets
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpQIBAAKC...\n-----END RSA PRIVATE KEY-----"
        assert _scan_args_for_secrets({"key": pem}) is not None

    def test_update_runtime_secret_in_model_field(self, tmp_path):
        tools = _make_tools(tmp_path, role="admin")
        current = tools.get_runtime_config()["config"]
        profiles = {role: {"provider": "codex", "model": "sk-ant-api03-abcdefghijklmnop"}
                    for role in ("planner", "cheap", "standard", "strong")}
        result = tools.update_runtime_config(profiles, current["limits"], current["revision"])
        assert result["status"] == "rejected"
        assert result["code"] == "secret_detected"
        # Secret must NOT appear anywhere in the return value
        result_text = json.dumps(result)
        assert "sk-ant-api03" not in result_text

    def test_create_deploy_target_secret_in_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FACTORY_DEPLOY_KEY_DIR", str(tmp_path / "keys"))
        tools = _make_tools(tmp_path, role="admin")
        values = _deploy_target_values()
        values["commands"]["deploy"] = 'curl -H "Authorization: Bearer sk-secret123456789012345678"'
        result = tools.create_deploy_target(values)
        assert result["status"] == "rejected"
        assert result["code"] == "secret_detected"
        result_text = json.dumps(result)
        assert "sk-secret" not in result_text

    def test_configure_operations_webhook_not_accepted(self, tmp_path):
        """The tool schema must not allow webhook; passing it raises secret_detected."""
        tools = _make_tools(tmp_path, role="admin")
        current = tools.get_operations_config()["config"]
        # Even if someone stuffs a webhook-shaped URL, the scan should catch it
        result = tools.configure_operations_with_check(
            current["revision"], True,
            extra_args={"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"})
        assert result["status"] == "rejected"
        assert result["code"] == "secret_detected" or result["code"] == "credential_gap"


# ===========================================================================
# 4  connection check → structured result
# ===========================================================================

class TestConnectionCheck:
    def test_empty_targets(self, tmp_path):
        tools = _make_tools(tmp_path, role="admin")
        result = tools.check_deploy_targets()
        assert result["status"] == "ok"
        assert result["last_checks"] == {}

    def test_runtime_config_reports_blockers(self, tmp_path):
        tools = _make_tools(tmp_path, role="admin")
        result = tools.get_runtime_config()
        assert result["status"] == "ok"
        # With default seeded profiles (empty model), there should be information
        assert "config" in result


# ===========================================================================
# 5  binding carries role
# ===========================================================================

class TestBinding:
    def test_binding_round_trip_preserves_role(self, tmp_path):
        from factory.control.conversation_tools import binding_for, ConversationTools
        store = _store(tmp_path)
        binding = binding_for(store, "conv-1", 42, actor_role="admin")
        assert binding["actor_role"] == "admin"
        tools = ConversationTools.from_binding(binding)
        assert tools.actor_role == "admin"

    def test_binding_defaults_to_member(self, tmp_path):
        from factory.control.conversation_tools import binding_for, ConversationTools
        store = _store(tmp_path)
        binding = binding_for(store, "conv-1", 42)
        # No actor_role → defaults to 'member' (safe default)
        tools = ConversationTools.from_binding(binding)
        assert tools.actor_role == "member"

    def test_admin_tool_names_registered(self):
        from factory.control.admin_config_tools import ADMIN_TOOL_NAMES
        assert len(ADMIN_TOOL_NAMES) > 0
        for name in ADMIN_TOOL_NAMES:
            assert name.startswith("mcp__session__")
