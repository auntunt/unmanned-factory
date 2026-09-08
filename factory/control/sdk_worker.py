"""Single-request child process for :mod:`factory.control.providers`."""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import asdict
from typing import Any

from .providers import (
    ProviderError,
    ProviderRequest,
    _run_claude,
    _run_codex,
    _run_dsh,
    _safe_json,
)


def _write(event_type: str, payload: Any) -> None:
    message = {"type": event_type, "payload": _safe_json(payload)}
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    line = sys.stdin.buffer.readline()
    if not line:
        _write("error", {"message": "worker expected one JSON request"})
        return 2
    try:
        raw = json.loads(line.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("request must be a JSON object")
        request = ProviderRequest(**raw)
        if request.provider == "claude":
            result = _run_claude(request, _write)
        elif request.provider == "codex":
            result = _run_codex(request, _write)
        elif request.provider == "dsh":
            result = _run_dsh(request, _write)
        else:
            raise ProviderError(f"unknown provider: {request.provider!r}")
        _write("complete", asdict(result))
        return 0
    except ProviderError as exc:
        _write("error", {"message": str(exc), "kind": type(exc).__name__})
        return 3
    except Exception as exc:  # SDK exceptions are provider failures, never success.
        _write("error", {"message": f"provider SDK failure: {exc}", "kind": type(exc).__name__})
        traceback.print_exc(file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
