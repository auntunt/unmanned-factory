"""workspace diff 捕获。

用 `git add -A -N` + `git diff HEAD`：
  - -N 只登记 intent-to-add，不 stage 内容 → 无副作用，人后续照常 commit
  - 这样才能拿到「新增文件」和「新目录里的新文件」的内容，单纯 git diff 拿不到
前提：workspace 至少有 1 个 commit，否则 HEAD 不存在。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def has_baseline(root: Path) -> bool:
    return _git(root, "rev-parse", "--verify", "HEAD").returncode == 0


def head_commit(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def capture_diff(root: Path) -> tuple[str, tuple[str, ...]]:
    if not has_baseline(root):
        raise RuntimeError(f"{root} 没有任何 commit，无法 diff。先 git commit 一个基线。")
    _git(root, "add", "-A", "-N")
    diff = _git(root, "diff", "HEAD").stdout
    names = _git(root, "diff", "HEAD", "--name-only").stdout
    paths = tuple(line for line in names.splitlines() if line.strip())
    return diff, paths


def diff_hash(diff: str) -> str | None:
    if not diff:
        return None
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "dist", "build"}


def neighbour_context(
    root: Path,
    changed_paths: tuple[str, ...],
    *,
    max_files: int = 12,
    max_bytes: int = 40_000,
) -> str:
    """改动文件的同目录既有代码，给架构监工判断约定和重复实现用。

    只取同目录、只取未改动的文件：改动本身在 diff 里已经给过一遍，
    重复给会让「哪些是新写的」变模糊，而这正是判重复实现要分清的。
    """
    changed = set(changed_paths)
    picked: list[str] = []
    for p in changed_paths:
        directory = (root / p).parent
        if not directory.is_dir():
            continue
        for f in sorted(directory.iterdir()):
            if not f.is_file() or f.suffix not in _CODE_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                continue
            if rel in changed or rel in picked:
                continue
            picked.append(rel)

    chunks: list[str] = []
    budget = max_bytes
    for rel in picked[:max_files]:
        try:
            body = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if budget <= 0:
            break
        chunks.append(f"--- {rel} ---\n{body[:budget]}")
        budget -= len(body)
    return "\n\n".join(chunks)
