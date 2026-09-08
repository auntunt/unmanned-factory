"""Fail-closed applicability checks for verified historical merge facts."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from factory.control.knowledge import KnowledgeStore


_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_CACHE_LIMIT = 32


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA40.fullmatch(value) is not None


def _same_fields(left: dict, right: dict) -> bool:
    """Compare only the immutable evidence fields, not version timestamps."""
    return all(left.get(field) == right.get(field) for field in (
        "content", "title", "paths", "commit_sha", "provenance",
    ))


def _canonical_merge(entry: object, project: dict, *, require_active: bool = True) -> str | None:
    if not isinstance(entry, dict) or not isinstance(project, dict):
        return None
    project_id = project.get("id")
    if not isinstance(project_id, str) or entry.get("project_id") != project_id:
        return None
    if entry.get("kind") != "fact" or (require_active and entry.get("status") != "active"):
        return None
    provenance = entry.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("source") != "github_merge":
        return None
    merge_sha = provenance.get("merge_commit_sha")
    if not _sha(merge_sha) or entry.get("commit_sha") != merge_sha:
        return None
    repository = project.get("repository")
    provenance_repository = provenance.get("repository")
    if (not isinstance(repository, str) or not isinstance(provenance_repository, str)
            or provenance_repository.casefold() != repository.casefold()):
        return None
    return merge_sha


def historical_merge_applicable(store, project, entry, baseline_sha, cache) -> bool:
    """Return whether a verified merge fact still applies to ``baseline_sha``.

    The current entry must be the unchanged latest version of an original
    GitHub-verified fact.  Only then is ancestry checked, and ancestry failures
    (including shallow/missing repositories, malformed input, and subprocess
    errors) are treated as ``False``.
    """
    merge_sha = _canonical_merge(entry, project)
    if merge_sha is None or not _sha(baseline_sha):
        return False
    if not isinstance(cache, dict):
        return False
    key = (merge_sha, baseline_sha)
    project_id = project.get("id")
    entry_key = entry.get("key") if isinstance(entry, dict) else None
    if not isinstance(entry_key, str) or not entry_key:
        return False
    try:
        versions = KnowledgeStore(store).versions(project_id, entry_key)
    except Exception:
        return False
    if not isinstance(versions, list) or not versions:
        return False
    original = versions[0]
    latest = versions[-1]
    if not isinstance(original, dict) or not isinstance(latest, dict):
        return False
    # The caller must be looking at the canonical latest row, while the first
    # row must remain an immutable GitHub merge fact.  Human edits retain the
    # old provenance in the store, so the exact-field check catches them.
    if (latest.get("project_id") != project_id or latest.get("key") != entry_key
            or latest.get("id") != entry.get("id") or latest.get("kind") != entry.get("kind")
            or latest.get("status") != entry.get("status") or not _same_fields(latest, entry)):
        return False
    original_sha = _canonical_merge(original, project, require_active=True)
    if original_sha != merge_sha or not _same_fields(original, entry):
        return False
    # Cache only the ancestry decision.  Authenticity and immutability are
    # always rechecked so an edited entry cannot piggyback on an old result.
    if key in cache:
        return cache[key] is True
    if len(cache) >= _CACHE_LIMIT:
        return False

    # A merge commit is trivially applicable to itself.  Avoiding this call is
    # safe after all database and scope checks above have passed.
    if merge_sha == baseline_sha:
        cache[key] = True
        return True
    try:
        root = project.get("workspace")
        if not isinstance(root, (str, Path)) or not Path(root).is_dir():
            cache[key] = False
            return False
        # Reserve the pair before invoking Git so even a timeout/error counts
        # against this context's bounded subprocess budget.
        cache[key] = False
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", merge_sha, baseline_sha],
            cwd=root, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    applicable = result.returncode == 0
    cache[key] = applicable
    return applicable
