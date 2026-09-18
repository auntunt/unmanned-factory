"""Tests for verification response JSON extraction — T13 red-then-green."""
import json
import pytest

from factory.control.verification import _extract_verdict_json


# ── helpers ──────────────────────────────────────────────────────────

_VALID_VERDICT = {
    "verdict": "pass",
    "reason": "All requirements verified directly",
    "criteria": [{"id": "c1", "status": "pass", "evidence": "checked"}],
    "acceptance_coverage": {"c1": "pass"},
    "operation_results": {},
}

_VALID_JSON = json.dumps(_VALID_VERDICT)


# ── Form 1: pure JSON (existing, must stay green) ───────────────────

def test_pure_json():
    result = _extract_verdict_json(_VALID_JSON)
    assert result["verdict"] == "pass"


# ── Form 2: full-text code fence (existing, must stay green) ────────

def test_full_fence_json_tag():
    text = "```json\n" + _VALID_JSON + "\n```"
    result = _extract_verdict_json(text)
    assert result["verdict"] == "pass"


def test_full_fence_no_tag():
    text = "```\n" + _VALID_JSON + "\n```"
    result = _extract_verdict_json(text)
    assert result["verdict"] == "pass"


# ── Form 3: explanation + unique JSON code block (NEW — must red before fix) ─

def test_explanation_then_single_json_fence():
    """Reproduces the real S1 failure: explanation text followed by a single
    ```json code fence containing a valid verdict object."""
    text = (
        "All requirements verified directly: standard-library-only counter.py "
        "and test_counter.py exist, tests pass, no external dependencies.\n\n"
        "```json\n" + _VALID_JSON + "\n```"
    )
    result = _extract_verdict_json(text)
    assert result["verdict"] == "pass"
    assert result["reason"] == "All requirements verified directly"
    assert result["acceptance_coverage"] == {"c1": "pass"}


def test_explanation_then_single_bare_fence():
    """Bare ``` (no json tag) with explanation prefix."""
    text = "Here is my review:\n\n```\n" + _VALID_JSON + "\n```"
    result = _extract_verdict_json(text)
    assert result["verdict"] == "pass"


# ── Rejection: multiple JSON blocks ────────────────────────────────

def test_two_json_fences_rejected():
    block = "```json\n" + _VALID_JSON + "\n```"
    text = "First:\n" + block + "\nSecond:\n" + block
    with pytest.raises(ValueError):
        _extract_verdict_json(text)


def test_two_fences_conflicting_verdict_rejected():
    pass_block = "```json\n" + json.dumps({**_VALID_VERDICT, "verdict": "pass"}) + "\n```"
    fail_block = "```json\n" + json.dumps({**_VALID_VERDICT, "verdict": "fail"}) + "\n```"
    text = "Analysis:\n" + pass_block + "\nRevised:\n" + fail_block
    with pytest.raises(ValueError):
        _extract_verdict_json(text)


# ── Rejection: no valid object ─────────────────────────────────────

def test_no_json_at_all():
    with pytest.raises(ValueError):
        _extract_verdict_json("This response contains no structured data.")


def test_fence_with_non_json():
    text = "```json\nnot valid json\n```"
    with pytest.raises(ValueError):
        _extract_verdict_json(text)


# ── Rejection: invalid verdict field ───────────────────────────────

def test_verdict_not_in_enum():
    bad = json.dumps({**_VALID_VERDICT, "verdict": "maybe"})
    with pytest.raises(ValueError):
        _extract_verdict_json(bad)


def test_reason_not_string():
    bad = json.dumps({**_VALID_VERDICT, "reason": 42})
    with pytest.raises(ValueError):
        _extract_verdict_json(bad)


# ── Post-parse rules NOT short-circuited ───────────────────────────

def test_parsed_verdict_still_has_coverage_fields():
    """Parsing succeeds but the caller still sees the full object — coverage
    rules are not bypassed by the extraction layer."""
    text = (
        "Checked everything.\n\n```json\n"
        + json.dumps({
            "verdict": "pass",
            "reason": "ok",
            "criteria": [],          # empty — downstream coverage will flag this
            "acceptance_coverage": {},
            "operation_results": {},
        })
        + "\n```"
    )
    result = _extract_verdict_json(text)
    # The extraction layer returns it; downstream coverage() decides fate.
    assert result["criteria"] == []
    assert result["acceptance_coverage"] == {}
