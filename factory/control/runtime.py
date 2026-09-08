"""Persistent provider runtime configuration and bounded diagnostics.

The runtime settings live beside the control store, rather than in process
environment, so a restart cannot silently reset an operator's configuration.
Provider diagnostics deliberately stop short of authentication or model calls:
the probe endpoint is the only operation which may invoke a provider.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.control.store import Conflict, Store, now

ROLES = ("planner", "cheap", "standard", "strong")
PROVIDERS = ("claude", "codex", "dsh")
DEFAULT_LIMITS = {
    "timeout_s": 600,
    "max_parallel": 2,
    "max_tasks": 20,
    "unknown_cost_policy": "stop",
}
_PROVIDER_MODULES = {
    "claude": ("claude_agent_sdk", "claude-agent-sdk"),
    "codex": ("openai_codex", "openai-codex"),
    "dsh": ("deepseek_harness", "deepseek-harness-sdk"),
}
_PROBE_CACHE_TTL = 10.0
_PROBE_TIMEOUT = 5.0
_SYSTEM_PROXY_WARNING = "系统启用代理但模型进程未配置代理，可能无法连接，请在服务启动环境设置 HTTP_PROXY、HTTPS_PROXY 或 ALL_PROXY。"
_inspect_cache_lock = threading.Lock()
_inspect_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


def _invalidate_inspect_cache(path: str) -> None:
    with _inspect_cache_lock:
        for key in tuple(_inspect_cache):
            if key[0] == path:
                _inspect_cache.pop(key, None)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_profiles() -> dict[str, dict[str, str]]:
    return {
        role: {
            "provider": os.getenv(f"FACTORY_{role.upper()}_PROVIDER", "codex"),
            "model": os.getenv(f"FACTORY_{role.upper()}_MODEL", ""),
        }
        for role in ROLES
    }


def _copy_profiles(value: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    source = value if value is not None else {}
    if not isinstance(source, Mapping):
        raise ValueError("profiles must be an object")
    result: dict[str, dict[str, str]] = {}
    for role in ROLES:
        raw = source.get(role, {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"profile {role!r} must be an object")
        result[role] = {
            "provider": str(raw.get("provider", "codex")),
            "model": str(raw.get("model", "")),
        }
    return result


def _validate_model(model: Any, role: str) -> str:
    if not isinstance(model, str):
        raise ValueError(f"profile {role!r} model must be a string")
    if len(model) > 160:
        raise ValueError(f"profile {role!r} model is too long")
    if any(ord(char) < 32 for char in model):
        raise ValueError(f"profile {role!r} model contains control characters")
    # Model identifiers may contain '/' (provider namespaces), but settings are
    # never a place to smuggle a URL, filesystem path, or shell command.
    lowered = model.casefold()
    if ("://" in lowered or model.startswith(('/', "./", "../"))
            or any(char.isspace() or char in ";|&$`\\" for char in model)):
        raise ValueError(f"profile {role!r} model must be a model identifier")
    return model


def _validate_profiles(value: Any, *, complete: bool = True) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError("profiles must be an object")
    if complete and set(value) != set(ROLES):
        raise ValueError("profiles must contain planner, cheap, standard, and strong")
    if not complete and not set(value).issubset(ROLES):
        raise ValueError("profiles contains an unknown role")
    result: dict[str, dict[str, str]] = {}
    for role in ROLES:
        raw = value.get(role, {"provider": "codex", "model": ""})
        if not isinstance(raw, Mapping):
            raise ValueError(f"profile {role!r} must be an object")
        if complete and set(raw) != {"provider", "model"}:
            raise ValueError(f"profile {role!r} must contain provider and model")
        provider = raw.get("provider", "codex")
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError(f"profile {role!r} provider must be claude, codex, or dsh")
        model = _validate_model(raw.get("model", ""), role)
        if role == "planner" and provider == "dsh":
            raise ValueError("dsh cannot be used for planner: no verified read-only capability")
        result[role] = {"provider": provider, "model": model}
    return result


def _validate_limits(value: Any, *, complete: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("limits must be an object")
    keys = set(DEFAULT_LIMITS)
    if complete and set(value) != keys:
        raise ValueError("limits must contain timeout_s, max_parallel, max_tasks, and unknown_cost_policy")
    if not complete and not set(value).issubset(keys):
        raise ValueError("limits contains an unknown setting")
    result = {**DEFAULT_LIMITS, **dict(value)}
    for key, low, high in (("timeout_s", 30, 1800), ("max_parallel", 1, 4), ("max_tasks", 1, 20)):
        item = result[key]
        if type(item) is not int or not low <= item <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if result["unknown_cost_policy"] not in ("stop", "allow_bounded"):
        raise ValueError("unknown_cost_policy must be stop or allow_bounded")
    return {key: result[key] for key in DEFAULT_LIMITS}


def _configuration(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("runtime configuration must be an object")
    if set(data) != {"profiles", "limits"}:
        raise ValueError("runtime configuration must contain profiles and limits")
    return {"profiles": _validate_profiles(data["profiles"]), "limits": _validate_limits(data["limits"])}


class RuntimeSettings:
    """CAS-updated immutable runtime configuration backed by SQLite."""

    def __init__(self, store: Store, profiles: Mapping[str, Any] | None = None,
                 limits: Mapping[str, Any] | None = None) -> None:
        self.store = store
        self._path = store.path
        self._initialize()
        with store.connect() as db:
            row = db.execute("SELECT revision FROM runtime_settings ORDER BY revision DESC LIMIT 1").fetchone()
        if row is None:
            seeded = _env_profiles() if profiles is None else profiles
            # A partial/empty seed is useful for first boot: the UI can fill in
            # all roles later. Environment is only read for this initial row.
            seed_profiles = _copy_profiles(seeded)
            config = {
                "profiles": _validate_profiles(seed_profiles),
                "limits": _validate_limits({**DEFAULT_LIMITS, **(dict(limits) if limits else {})}),
            }
            self._insert_initial(config)

    def _initialize(self) -> None:
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    revision INTEGER PRIMARY KEY,
                    profiles TEXT NOT NULL,
                    limits TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_settings_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    data TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_probes (
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    configuration_revision INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_probe_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    probe_id TEXT NOT NULL,
                    configuration_revision INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS no_runtime_settings_update BEFORE UPDATE ON runtime_settings
                    BEGIN SELECT RAISE(ABORT,'runtime settings are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_runtime_settings_delete BEFORE DELETE ON runtime_settings
                    BEGIN SELECT RAISE(ABORT,'runtime settings are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_runtime_audit_update BEFORE UPDATE ON runtime_settings_audit
                    BEGIN SELECT RAISE(ABORT,'runtime audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_runtime_audit_delete BEFORE DELETE ON runtime_settings_audit
                    BEGIN SELECT RAISE(ABORT,'runtime audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_runtime_probe_update BEFORE UPDATE ON runtime_probes
                    BEGIN SELECT RAISE(ABORT,'runtime probes are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_runtime_probe_delete BEFORE DELETE ON runtime_probes
                    BEGIN SELECT RAISE(ABORT,'runtime probes are immutable'); END;
                """
            )

    def _insert_initial(self, config: Mapping[str, Any]) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runtime_settings LIMIT 1").fetchone() is not None:
                return
            at = _iso()
            db.execute("INSERT INTO runtime_settings VALUES (?,?,?,?,?)",
                       (1, json.dumps(config["profiles"], ensure_ascii=False),
                        json.dumps(config["limits"], ensure_ascii=False), at, "system"))
            db.execute("INSERT INTO runtime_settings_audit(revision,actor,action,data,at) VALUES (?,?,?,?,?)",
                       (1, "system", "seed", json.dumps(config, ensure_ascii=False), at))

    def get(self) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT revision,profiles,limits,updated_at FROM runtime_settings ORDER BY revision DESC LIMIT 1").fetchone()
        if row is None:  # pragma: no cover - constructor always seeds
            raise RuntimeError("runtime settings are not initialized")
        return {"revision": int(row[0]), "profiles": json.loads(row[1]),
                "limits": json.loads(row[2]), "updated_at": row[3]}

    def history(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            rows = db.execute("SELECT revision,actor,action,data,at FROM runtime_settings_audit WHERE action IN ('seed','update') ORDER BY id").fetchall()
        return [{"revision": int(row[0]), "actor": row[1], "action": row[2],
                 **json.loads(row[3]), "updated_at": row[4]} for row in rows]

    def update(self, data: Mapping[str, Any], expected_revision: int, actor: str) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor is required")
        config = _configuration(data)
        at = _iso()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM runtime_settings ORDER BY revision DESC LIMIT 1").fetchone()
            current = int(row[0]) if row else 0
            if current != expected_revision:
                raise Conflict("运行配置已更新，请重新加载后再保存")
            revision = current + 1
            db.execute("INSERT INTO runtime_settings VALUES (?,?,?,?,?)",
                       (revision, json.dumps(config["profiles"], ensure_ascii=False),
                        json.dumps(config["limits"], ensure_ascii=False), at, actor.strip()))
            db.execute("INSERT INTO runtime_settings_audit(revision,actor,action,data,at) VALUES (?,?,?,?,?)",
                       (revision, actor.strip(), "update", json.dumps(config, ensure_ascii=False), at))
        return {"revision": revision, **config, "updated_at": at}

    def record_probe(self, result: Mapping[str, Any]) -> dict[str, Any]:
        required = ("id", "profile", "provider", "model", "configuration_revision", "checked_at", "outcome", "message")
        if any(key not in result for key in required):
            raise ValueError("incomplete runtime probe")
        clean = {key: result[key] for key in required}
        with self.store.connect() as db:
            db.execute("INSERT INTO runtime_probes VALUES (?,?,?,?,?,?,?,?)", tuple(clean[key] for key in required))
            db.execute("INSERT INTO runtime_probe_audit(probe_id,configuration_revision,outcome,at) VALUES (?,?,?,?)",
                       (clean["id"], clean["configuration_revision"], clean["outcome"], clean["checked_at"]))
        _invalidate_inspect_cache(self.store.path)
        return clean

    def last_probes(self, revision: int | None = None) -> list[dict[str, Any]]:
        revision = revision if revision is not None else self.get()["revision"]
        with self.store.connect() as db:
            rows = db.execute("""SELECT id,profile,provider,model,configuration_revision,checked_at,outcome,message
                FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY profile ORDER BY checked_at DESC, rowid DESC) AS position
                      FROM runtime_probes WHERE configuration_revision=?)
                WHERE position=1 ORDER BY checked_at DESC""", (revision,)).fetchall()
        return [{"id": row[0], "profile": row[1], "provider": row[2], "model": row[3],
                 "configuration_revision": int(row[4]), "checked_at": row[5], "outcome": row[6], "message": row[7]} for row in rows]


def _module_present(provider: str) -> bool:
    try:
        return importlib.util.find_spec(_PROVIDER_MODULES[provider][0]) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _sdk_inspect(provider: str) -> dict[str, Any]:
    module, distribution = _PROVIDER_MODULES[provider]
    installed = _module_present(provider)
    version = None
    runtime_version = None
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        pass
    if provider == "dsh":
        try:
            runtime_version = importlib.metadata.version("deepseek-harness-runtime-bin")
        except importlib.metadata.PackageNotFoundError:
            pass
    if not installed:
        return {"installed": False, "version": version, "runtime_version": runtime_version,
                "import_status": "missing", "compatible": False}
    attrs = {
        "claude": ["ClaudeAgentOptions", "HookMatcher", "PermissionResultAllow", "PermissionResultDeny", "query"],
        "codex": ["Codex", "Sandbox", "ApprovalMode"],
        "dsh": ["DeepSeekHarness"],
    }[provider]
    runtime_check = {
        "codex": ["from codex_cli_bin import bundled_codex_path", "paths=[bundled_codex_path()]"],
        "claude": ["from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport",
                   "paths=[SubprocessCLITransport._find_cli(object.__new__(SubprocessCLITransport))]"],
        "dsh": ["from deepseek_harness_runtime import resolve_bundled_launch_args",
                "args=resolve_bundled_launch_args()", "paths=[args[0]]"],
    }[provider]
    script = "\n".join([
        "import importlib, json",
        "from pathlib import Path",
        "import os",
        f"m=importlib.import_module({module!r})",
        f"required={attrs!r}",
        "missing=[x for x in required if not hasattr(m,x)]",
        "ok=not missing",
        "if 'ApprovalMode' in required: ok = ok and getattr(getattr(m,'ApprovalMode',None),'deny_all',None) is not None",
        "runtime_ok=False",
        "try:",
        *(" " + line for line in runtime_check),
        " runtime_ok=all(Path(p).is_file() and os.access(p, os.X_OK) for p in paths)",
        "except Exception:",
        " pass",
        "print(json.dumps({'ok':ok,'missing':missing,'runtime_ok':runtime_ok}))",
    ])
    try:
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"installed": True, "version": version, "runtime_version": runtime_version,
                "import_status": "error", "compatible": False,
                "error": type(exc).__name__}
    if proc.returncode != 0:
        return {"installed": True, "version": version, "runtime_version": runtime_version,
                "import_status": "error", "compatible": False,
                "error": "sdk import failed"}
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, ValueError):
        payload = {"ok": False, "missing": ["invalid introspection result"]}
    return {"installed": True, "version": version, "runtime_version": runtime_version,
            "import_status": "ok" if payload.get("ok") else "incompatible",
            "compatible": bool(payload.get("ok")) and bool(payload.get("runtime_ok")),
            "runtime_ok": bool(payload.get("runtime_ok", False))}


def _binary_available(name: str) -> bool:
    path = shutil.which(name)
    return bool(path and Path(path).is_file() and os.access(path, os.X_OK))


def _auth_hint(provider: str) -> str:
    keys = {
        "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        "codex": ("OPENAI_API_KEY",),
        "dsh": ("DEEPSEEK_API_KEY", "DSH_API_KEY"),
    }[provider]
    return "present" if any(os.getenv(key) for key in keys) else "unknown"


def _auth_sources(provider: str) -> list[str]:
    """Check known credential locations without opening them or showing paths."""
    sources = []
    if _auth_hint(provider) == "present":
        sources.append("environment")
    candidates = {
        "codex": [Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "auth.json"],
        "claude": [Path(os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"],
        # DSH may use multiple provider configurations; absence is unknown.
        "dsh": [],
    }[provider]
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.R_OK):
                sources.append("credential_file")
                break
        except OSError:
            pass
    return sources


def _proxy_environment_configured() -> bool:
    """Whether this process has an explicit proxy without exposing its value."""
    return any(bool(os.getenv(name, "").strip()) for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ))


def _macos_system_proxy_without_process_proxy() -> bool:
    """Read the macOS proxy switch only; never inspect or report proxy URLs."""
    if sys.platform != "darwin" or _proxy_environment_configured():
        return False
    try:
        result = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True,
                                timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    # scutil's dictionary reports HTTPEnable / HTTPSEnable independently.
    # We retain only the boolean signal, not its port or endpoint.
    import re
    return bool(re.search(r"(?m)^\s*HTTPS?Enable\s*:\s*1\s*$", result.stdout or ""))


def profile_blockers(profile: Mapping[str, Any], role: str) -> list[str]:
    """Return only deterministic blockers; absence of env auth is unknown."""
    blockers: list[str] = []
    if role not in ROLES:
        blockers.append(f"unknown runtime role: {role}")
        return blockers
    if not isinstance(profile, Mapping):
        return [f"profile {role} is not configured"]
    provider = profile.get("provider")
    model = profile.get("model")
    if provider not in PROVIDERS:
        blockers.append(f"profile {role} has unsupported provider")
        return blockers
    if not isinstance(model, str) or not model.strip():
        blockers.append(f"profile {role} model is not configured")
    if not _module_present(provider):
        blockers.append(f"{provider} SDK is not installed")
    else:
        local = _sdk_inspect(provider)
        if local["import_status"] != "ok":
            blockers.append(f"{provider} SDK API is unavailable or incompatible")
        if local.get("runtime_ok") is False:
            blockers.append(f"{provider} SDK runtime executable is unavailable")
    if provider == "dsh" and role == "planner":
        blockers.append("dsh cannot plan: no verified read-only capability")
    elif provider == "dsh" and not os.getenv("DSH_HOME", "").strip():
        blockers.append("DSH_HOME is not configured")
    return blockers


def _inspect_runtime_uncached(settings: RuntimeSettings, workspace_root: str | Path | None = None,
                              static_dir: str | Path | None = None) -> dict[str, Any]:
    config = settings.get()
    tools: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        sdk = _sdk_inspect(provider)
        compatible = bool(sdk["compatible"])
        issues: list[dict[str, str]] = []
        if not sdk["installed"]:
            issues.append({"code": "sdk_missing", "message": f"{provider} SDK is not installed", "remediation_id": f"install_{provider}"})
        elif not compatible:
            issues.append({"code": "sdk_incompatible", "message": f"{provider} SDK import/API compatibility check failed", "remediation_id": f"install_{provider}"})
        if sdk["installed"] and sdk.get("runtime_ok") is False:
            issues.append({"code": "runtime_missing", "message": f"{provider} SDK runtime executable is unavailable", "remediation_id": f"install_{provider}"})
        if provider == "dsh" and not os.getenv("DSH_HOME", "").strip():
            issues.append({"code": "dsh_home_missing", "message": "DSH_HOME is not configured", "remediation_id": "configure_dsh_home"})
        tools.append({"id": provider, "installed": bool(sdk["installed"]), "version": sdk["version"],
                      **({"runtime_version": sdk["runtime_version"]} if provider == "dsh" else {}),
                      "import_status": sdk["import_status"],
                      "capabilities": {"read_only": compatible and provider != "dsh", "workspace_write": compatible},
                      "runtime_status": "available" if sdk.get("runtime_ok") else "missing",
                      "auth": "present" if _auth_sources(provider) else "unknown",
                      "auth_sources": _auth_sources(provider), "issues": issues})
    by_provider = {tool["id"]: tool for tool in tools}
    blockers: list[str] = []
    planning = True
    execution = True
    for role, profile in config["profiles"].items():
        # The provider was already inspected above; avoid importing each SDK
        # again for every role during one GET.
        role_blockers = [] if profile.get("model") else [f"profile {role} model is not configured"]
        if profile.get("provider") == "dsh" and not os.getenv("DSH_HOME", "").strip():
            role_blockers.append("DSH_HOME is not configured")
        tool = by_provider.get(profile.get("provider"))
        if tool and tool["import_status"] != "ok":
            role_blockers.append(f"{profile.get('provider')} SDK is not API-compatible")
        if role == "planner" and tool and not tool["capabilities"]["read_only"]:
            role_blockers.append(f"{profile.get('provider')} does not provide verified read-only planning")
        if tool and not tool["capabilities"]["workspace_write"]:
            role_blockers.append(f"{profile.get('provider')} does not provide workspace write capability")
        if role == "planner" and role_blockers:
            planning = False
        if role != "planner" and role_blockers:
            execution = False
        blockers.extend(f"{role}: {reason}" for reason in dict.fromkeys(role_blockers))
    git_available = _binary_available("git")
    node_available = _binary_available("node")
    hermes_available = _binary_available("hermes")
    frontend_built = bool(static_dir and (Path(static_dir) / "index.html").is_file())
    if not git_available:
        blockers.append("git is unavailable")
        planning = execution = False
    publishing = bool(os.getenv("FACTORY_GITHUB_TOKEN")) and git_available
    if not publishing:
        blockers.append("GitHub publishing token is not present")
    probes = settings.last_probes(config["revision"])
    latest = {}
    for probe in probes:
        latest.setdefault(probe["profile"], probe)
    live_verified = {
        role: bool(role in latest and latest[role]["outcome"] == "passed"
                   and latest[role]["provider"] == profile["provider"]
                   and latest[role]["model"] == profile["model"])
        for role, profile in config["profiles"].items()
    }
    verification_note = "Connection tests describe the last observed result at checked_at; credentials and access may change. GitHub publishing has not been tested."
    if _macos_system_proxy_without_process_proxy():
        verification_note += " " + _SYSTEM_PROXY_WARNING
    return {
        "checked_at": _iso(),
        "execution_mode": "local_sdk_children",
        "configuration_revision": config["revision"],
        "profiles": config["profiles"],
        "limits": config["limits"],
        "tools": tools,
        "host": {"python_version": sys.version.split()[0], "git_available": git_available,
                 "node_available": node_available, "hermes_available": hermes_available,
                 "frontend_built": frontend_built},
        "local_readiness": {"planning": planning, "execution": execution, "publishing": publishing},
        "readiness": {"planning": planning and live_verified["planner"],
                      "execution": execution and all(live_verified[r] for r in ROLES if r != "planner"),
                      "publishing": False},
        "live_verified": live_verified,
        "verification_note": verification_note,
        "blockers": list(dict.fromkeys(blockers)),
        "last_probes": probes,
    }


def inspect_runtime(settings: RuntimeSettings, workspace_root: str | Path | None = None,
                    static_dir: str | Path | None = None) -> dict[str, Any]:
    """Return bounded diagnostics, cached briefly to keep GET inexpensive."""
    config = settings.get()
    # Proxy variables can be added to a service environment without changing
    # its runtime configuration; do not serve the old warning from cache.
    proxy_environment = tuple(bool(os.getenv(name, "").strip()) for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    ))
    key = (settings.store.path, config["revision"], str(workspace_root or ""), str(static_dir or ""), proxy_environment)
    current = time.monotonic()
    with _inspect_cache_lock:
        cached = _inspect_cache.get(key)
        if cached and current - cached[0] <= _PROBE_CACHE_TTL:
            # A caller must not mutate the shared cache object.
            return json.loads(json.dumps(cached[1]))
    result = _inspect_runtime_uncached(settings, workspace_root, static_dir)
    with _inspect_cache_lock:
        _inspect_cache[key] = (time.monotonic(), result)
    return json.loads(json.dumps(result))


__all__ = ["RuntimeSettings", "inspect_runtime", "profile_blockers", "ROLES", "PROVIDERS"]
