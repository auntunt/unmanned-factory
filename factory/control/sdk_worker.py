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
    failure_metadata,
)


def _write(event_type: str, payload: Any) -> None:
    clean = _safe_json(payload)
    # Result text is a structured response, not a preview event. Truncating it
    # at the log-preview limit corrupts otherwise valid large Analysis JSON.
    result = payload if event_type == 'complete' else payload.get('partial_result') if event_type == 'error' else None
    if isinstance(result, dict) and isinstance(result.get('text'), str) and len(result['text']) <= 128_000:
        target = clean if event_type == 'complete' else clean['partial_result']
        target['text'] = result['text']
    message = {"type": event_type, "payload": clean}
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _failure_payload(exc):
    metadata = failure_metadata(exc)
    for key in ('transient', 'error_kind', 'status_code'):
        if getattr(exc, key, None) is not None:
            metadata[key] = getattr(exc, key)
    return {'message': str(exc), 'kind': type(exc).__name__,
            'session_id': getattr(exc, 'session_id', None),
            **({'partial_result': asdict(exc.partial_result)} if getattr(exc, 'partial_result', None) is not None else {}), **metadata}


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
        _write("error", _failure_payload(exc))
        return 3
    except Exception as exc:  # SDK exceptions are provider failures, never success.
        _write("error", {**_failure_payload(exc), "message": f"provider SDK failure: {exc}"})
        traceback.print_exc(file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
