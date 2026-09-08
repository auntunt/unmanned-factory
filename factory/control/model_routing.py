"""Deterministic worker-model selection for bounded execution attempts.

The runtime configuration supplies role names, providers and actual model IDs.
This module deliberately does not invent availability or silently substitute a
different role when the configured role is unusable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RoutingError(ValueError):
    """A required configured model profile cannot safely be dispatched."""


def _base_profile(task: Mapping[str, Any]) -> tuple[str, str]:
    """Return the compatible initial role and a durable selection reason."""
    from factory.control.planning import profile_for

    normalized = dict(task)
    normalized["paths"] = list(task.get("paths") or ())
    normalized["checks"] = list(task.get("checks") or ())
    # Execution appends fixed context/capability reference data to the worker
    # prompt. Those instructions explicitly cannot change models or budgets;
    # treating words such as "permissions" in that data as task risk would
    # silently turn every bounded task into a strong-model dispatch.
    prompt = normalized.get("prompt")
    if isinstance(prompt, str):
        for marker in ("\n\nPROJECT REFERENCE DATA (UNTRUSTED):", "\n\nProject-configured capability contracts"):
            prompt = prompt.split(marker, 1)[0]
        normalized["prompt"] = prompt
    profile = str(profile_for(normalized))
    if profile == "strong":
        if task.get("risk") == "high":
            return profile, "high-risk task requires the strong configured profile"
        return profile, "large or high-risk task requires the strong configured profile"
    if profile == "cheap":
        return profile, "small low-risk bounded task uses the economical configured profile"
    return profile, "cross-module or medium-complexity task uses the standard configured profile"


def _escalated_profile(initial: str, attempt: int, auto_escalate: bool) -> tuple[str, str]:
    if attempt == 1:
        return initial, ""
    if not auto_escalate:
        return initial, "automatic escalation is disabled; retrying the configured profile"
    if initial == "cheap":
        if attempt == 2:
            return ("standard", "previous economical attempt failed; escalating to the standard configured profile")
        return ("strong", "previous repair attempts failed; escalating to the strong configured profile")
    if initial == "standard":
        return ("strong", "previous standard attempt failed; escalating to the strong configured profile")
    return ("strong", "previous strong attempt failed; retrying the strongest configured profile")


def select_profile(
    task: Mapping[str, Any], profiles: Mapping[str, Any], *, attempt: int = 1, auto_escalate: bool = True
) -> dict[str, Any]:
    """Choose a configured role for one attempt, or report a visible error.

    Attempt one follows the existing task complexity/risk policy. Subsequent
    attempts use a stronger configured role where one exists.  The caller owns
    retry eligibility and limits; this pure function only explains selection.
    """
    if not isinstance(task, Mapping):
        raise RoutingError("task must be an object")
    if not isinstance(profiles, Mapping):
        raise RoutingError("configured profiles must be an object")
    if type(attempt) is not int or attempt < 1:
        raise RoutingError("attempt must be a positive integer")
    if not isinstance(auto_escalate, bool):
        raise RoutingError("auto_escalate must be a boolean")

    initial, initial_reason = _base_profile(task)
    profile, retry_reason = _escalated_profile(initial, attempt, auto_escalate)
    config = profiles.get(profile)
    if not isinstance(config, Mapping):
        raise RoutingError(f"profile {profile!r} is not configured")
    provider = config.get("provider")
    model = config.get("model")
    if (not isinstance(provider, str) or not provider.strip()
            or not isinstance(model, str) or not model.strip()):
        raise RoutingError(f"profile {profile!r} lacks provider/model")
    return {
        "profile": profile,
        "provider": provider,
        "model": model,
        "reason": retry_reason or initial_reason,
        "attempt": attempt,
    }
