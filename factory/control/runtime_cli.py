"""Operator-facing runtime diagnostics.

This module deliberately contains no provider calls.  ``doctor`` reports the
same bounded inspection used by the control plane and separates package
installation from the provider's authentication hint.  It is safe to run in
CI, during an install dry-run, or when credentials are not configured.

The runtime implementation is imported lazily so an old checkout can still
print command help before the runtime migration is installed.  The production
entry point is added in ``pyproject.toml`` by the integration owner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


try:  # The runtime module lands with the control-plane integration.
    from factory.control.runtime import RuntimeSettings, inspect_runtime
except (ImportError, SyntaxError):  # pragma: no cover - pre-runtime checkouts
    RuntimeSettings = None  # type: ignore[assignment,misc]
    inspect_runtime = None  # type: ignore[assignment]


_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)


def _scrub(value: Any, *, key: str = "") -> Any:
    """Return bounded JSON-safe data without exposing credential material."""

    if _SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _scrub(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        # Diagnostics should not become an accidental output channel for an
        # enormous SDK object or an exception containing a secret.
        if isinstance(value, str) and len(value) > 8_000:
            return value[:8_000] + "...[truncated]"
        return value
    return str(value)[:8_000]


def _default_db() -> Path:
    data = Path(os.getenv("FACTORY_CONTROL_DATA", "~/.factory/control")).expanduser()
    return Path(os.getenv("FACTORY_CONTROL_DB", str(data / "control.db"))).expanduser()


def _default_static_dir(workspace: Path) -> Path:
    return Path(os.getenv("FACTORY_STATIC_DIR", str(workspace / "frontend" / "dist"))).expanduser()


def _make_settings(db: Path):
    if RuntimeSettings is None:
        raise RuntimeError(
            "factory.control.runtime is not installed in this checkout; "
            "complete the v2 runtime update before running doctor"
        )
    # Importing Store here keeps ``factory-runtime --help`` independent from
    # the optional SDKs, while RuntimeSettings retains the app's SQLite store.
    from factory.control.store import Store

    # A doctor invoked outside systemd does not inherit EnvironmentFile. Never
    # seed or migrate the live database from that incomplete environment.
    temporary = tempfile.TemporaryDirectory(prefix="factory-doctor-")
    snapshot = Path(temporary.name) / "control.db"
    try:
        if db.exists():
            with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(snapshot) as destination:
                    source.backup(destination)
        settings = RuntimeSettings(Store(snapshot))
        settings._doctor_temporary = temporary
        return settings
    except Exception:
        temporary.cleanup()
        raise


def run_doctor(
    *,
    db: str | Path | None = None,
    workspace: str | Path | None = None,
    static_dir: str | Path | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Run the no-paid-call runtime inspection and return a JSON-safe report."""

    workspace_root = Path(workspace or os.getenv("FACTORY_WORKSPACE_ROOT", ".")).expanduser().resolve()
    static = Path(static_dir).expanduser().resolve() if static_dir else _default_static_dir(workspace_root).resolve()
    runtime_settings = settings or _make_settings(Path(db).expanduser() if db else _default_db())
    if inspect_runtime is None:
        raise RuntimeError(
            "factory.control.runtime is not installed in this checkout; "
            "complete the v2 runtime update before running doctor"
        )
    try:
        report = inspect_runtime(runtime_settings, workspace_root, static)
        report = {**report, "diagnostic_context": "Current CLI user/environment; service credentials may differ. Live database is not modified."}
        return _scrub(report)
    finally:
        if settings is None:
            temporary = getattr(runtime_settings, "_doctor_temporary", None)
            if temporary is not None:
                temporary.cleanup()


def _profile_line(profile: Any) -> str:
    if not isinstance(profile, dict):
        return str(profile)
    provider = profile.get("provider") or "(未配置)"
    model = profile.get("model") or "(未配置)"
    return f"{provider} / {model}"


def _print_human(report: dict[str, Any]) -> None:
    """Print a compact operator report; JSON remains the machine interface."""

    print("Factory runtime doctor (no paid calls)")
    print(f"checked_at             : {report.get('checked_at', 'unknown')}")
    print(f"configuration_revision : {report.get('configuration_revision', 'unknown')}")
    print("\nProfiles:")
    profiles = report.get("profiles") or {}
    for role in ("planner", "cheap", "standard", "strong"):
        print(f"  {role:<9} {_profile_line(profiles.get(role, {}))}")

    print("\nTools (package and auth hint are separate):")
    for tool in report.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        installed = "installed" if tool.get("installed") else "missing"
        imported = tool.get("import_status", "unknown")
        auth = tool.get("auth", "unknown")
        print(f"  {tool.get('id', 'unknown'):<9} package={installed}; import={imported}; runtime={tool.get('runtime_status', 'unknown')}; auth_hint={auth}")
        for issue in tool.get("issues") or []:
            if isinstance(issue, dict) and issue.get("message"):
                print(f"             issue: {issue['message']}")

    host = report.get("host") or {}
    print("\nHost:")
    for key in ("python_version", "git_available", "node_available", "hermes_available", "frontend_built"):
        if key in host:
            print(f"  {key:<18}: {host[key]}")
    readiness = report.get("readiness") or {}
    print("\nReadiness:")
    for key in ("planning", "execution", "publishing"):
        if key in readiness:
            print(f"  {key:<12}: {readiness[key]}")
    blockers = report.get("blockers") or []
    print("\nBlockers:")
    if blockers:
        for blocker in blockers:
            print(f"  - {blocker}")
    else:
        print("  - none")
    print("\nAuthentication is reported as a bounded hint only; doctor never performs a provider request.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-runtime",
        description="Inspect local runtime readiness without making paid provider calls.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="report installation, host, and runtime readiness")
    doctor.add_argument("--json", action="store_true", help="emit one sanitized JSON document")
    doctor.add_argument("--db", default=None, metavar="PATH", help="control-plane SQLite database")
    doctor.add_argument("--workspace", default=None, metavar="PATH", help="checkout to inspect")
    doctor.add_argument("--static-dir", default=None, metavar="PATH", help="built frontend directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "doctor":  # pragma: no cover - argparse enforces this
        return 2
    try:
        report = run_doctor(db=args.db, workspace=args.workspace, static_dir=args.static_dir)
    except Exception as exc:
        # Keep errors useful while avoiding an exception repr that may contain
        # provider configuration.  The detailed, scrubbed issues belong in the
        # runtime report once the runtime module is available.
        message = f"doctor failed: {type(exc).__name__}; check database permissions and runtime installation"
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False, sort_keys=True))
        else:
            print(message, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
