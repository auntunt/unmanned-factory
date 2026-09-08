"""A small, deterministic code graph built from immutable Git objects.

The indexer deliberately does not inspect the checkout.  The checkout is only
used as the cwd for read-only Git object commands, and all content comes from
the commit named by ``refs/heads/<base_branch>``.
"""
from __future__ import annotations

import ast
import json
import os
import posixpath
import re
import selectors
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from factory.control.store import scrub

SCHEMA_VERSION = 1
PARSER_VERSION = "codegraph-1"
MAX_FILES = 500
MAX_FILE_BYTES = 128 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_NODES = 5000
MAX_EDGES = 10000
MAX_SNIPPET = 1200
MAX_LS_BYTES = 8 * 1024 * 1024
GIT_TIMEOUT = 15
INDEX_TIMEOUT = 60

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_IDENT = r"[A-Za-z_$][\w$]*"
_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_ALLOWED_EXTS = _JS_EXTS | {".py", ".json", ".md"}
_GENERATED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "out",
    "target", "coverage", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv",
    "venv", "env", "generated", "gen", "fixtures", ".next", ".cache", ".tox",
    ".ruff_cache", "site-packages", "bower_components", "deps", "third_party",
}
_AGENT_DIRS = {".claude", ".codex", "hooks", ".github-hooks"}
_SECRET_WORDS = re.compile(
    r"(?i)(?:secret|credential|password|passwd|token|api[_-]?key|access[_-]?key|private[_-]?key|id_rsa|ssh_key)"
)


def _project_root(project: dict[str, Any]) -> Path:
    try:
        raw = project["workspace"]
    except (KeyError, TypeError) as exc:
        raise ValueError("项目缺少仓库目录") from exc
    root = Path(str(raw)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("仓库目录不存在")
    return root


def _git_run(root: Path, args: list[str], *, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a fixed, non-shell Git command."""
    try:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git 仓库读取失败") from exc


def baseline_sha(project: dict[str, Any]) -> str:
    """Resolve only the project's local ``refs/heads/<base_branch>`` ref."""
    root = _project_root(project)
    branch = str(project.get("base_branch", "main"))
    if not branch or "\x00" in branch or branch.startswith("-") or "\n" in branch:
        raise ValueError("基线分支名无效")
    checked = _git_run(root, ["check-ref-format", "--branch", branch])
    if checked.returncode:
        raise ValueError("基线分支名无效")
    ref = f"refs/heads/{branch}"
    result = _git_run(root, ["rev-parse", "--verify", ref])
    sha = result.stdout.decode("ascii", "ignore").strip()
    if result.returncode or not _HEX40.fullmatch(sha):
        raise ValueError("基线分支不存在或不是提交")
    return sha


def _tree_records(root: Path, commit_sha: str) -> tuple[list[tuple[str, str, str, str]], int, dict[str, int]]:
    """Read a bounded NUL-separated ls-tree stream.

    The command output is capped before it can become an unbounded Python
    string. We keep only the first MAX_FILES eligible records, while still
    consuming the stream so a very large tree fails explicitly. Generated and
    sensitive paths therefore do not consume the eligible-file budget.
    """
    try:
        proc = subprocess.Popen(["git", "ls-tree", "-r", "-z", "--full-tree", commit_sha],
                                cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ValueError("Git tree 读取失败") from exc
    assert proc.stdout is not None and proc.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    total = 0
    out = bytearray()
    err = bytearray()
    deadline = time.monotonic() + GIT_TIMEOUT
    too_large = False
    try:
        while selector.get_map():
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise ValueError("Git tree 读取超时")
            for key, _ in selector.select(timeout=max(0.01, min(0.25, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "out":
                    total += len(chunk)
                    if total > MAX_LS_BYTES:
                        too_large = True
                        proc.kill()
                        break
                    out.extend(chunk)
                elif len(err) < 65536:
                    err.extend(chunk[: 65536 - len(err)])
            if too_large:
                break
        if too_large:
            proc.wait(timeout=2)
            raise ValueError("Git tree 输出超过安全上限")
        code = proc.wait(timeout=2)
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    if code:
        raise ValueError("Git tree 读取失败")

    result: list[tuple[str, str, str, str]] = []
    count = 0
    eligible = 0
    skipped: dict[str, int] = {}
    for record in bytes(out).split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, obj_type, sha = header.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        count += 1
        if mode == "120000" or obj_type == "symlink":
            skipped["symlink"] = skipped.get("symlink", 0) + 1
            continue
        if mode == "160000" or obj_type == "commit":
            skipped["submodule"] = skipped.get("submodule", 0) + 1
            continue
        if not _allowed_path(path):
            skipped["path"] = skipped.get("path", 0) + 1
            continue
        eligible += 1
        if len(result) < MAX_FILES:
            result.append((mode, obj_type, sha, path))
    if eligible > MAX_FILES:
        skipped["file_limit"] = eligible - MAX_FILES
    return result, count, skipped


def _sensitive_path(path: str) -> bool:
    p = PurePosixPath(path)
    parts = [x.lower() for x in p.parts]
    if not parts or any(x in _GENERATED_DIRS or x in _AGENT_DIRS for x in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith(".env") or name.endswith(".env") or name.startswith(("config.", "credentials.", "secret.")) or name in {"config", "credentials", "secret"}:
        return True
    if _SECRET_WORDS.search(name):
        return True
    if name.endswith((".pem", ".key", ".crt", ".cer", ".der", ".p12", ".pfx")):
        return True
    if name in {"id_rsa", "id_ed25519", "known_hosts"}:
        return True
    if any(x in {"config", "configs", ".config", "secrets", "credentials"} for x in parts[:-1]):
        return True
    return False


def _allowed_path(path: str) -> bool:
    if "\x00" in path or path.startswith("/") or any(x in ("", ".", "..") for x in path.split("/")):
        return False
    if _sensitive_path(path):
        return False
    return PurePosixPath(path).suffix.lower() in _ALLOWED_EXTS


def _snippet(text: str, start: int, end: int | None = None) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    s = max(1, start) - 1
    e = min(len(lines), end or start)
    return scrub("\n".join(lines[s:e]))[:MAX_SNIPPET]


def _node(kind: str, path: str, name: str, line: int, end_line: int, language: str,
          snippet: str = "", resolution: str = "syntax") -> dict[str, Any]:
    # File IDs are intentionally short and stable; symbol IDs include the
    # symbol name and declaration line to avoid collisions.
    ident = f"file:{path}" if kind == "file" else f"{kind}:{path}:{name}:{line}"
    return {"id": ident, "kind": kind, "path": path, "name": name,
            "line": max(1, line), "end_line": max(line, end_line),
            "language": language, "snippet": scrub(snippet)[:MAX_SNIPPET],
            "resolution": resolution}


def _edge(source: str, target: str, kind: str, path: str, line: int,
          resolution: str = "syntax") -> dict[str, Any]:
    return {"source": source, "target": target, "kind": kind, "resolution": resolution,
            "path": path, "line": max(1, line)}


def _module_target(source_path: str, module: str, file_paths: set[str], *, relative: int | bool = False) -> str | None:
    if not module:
        return None
    base = PurePosixPath(source_path).parent
    if relative:
        level = 1 if relative is True else int(relative)
        for _ in range(max(0, level - 1)):
            base = base.parent
        if "/" in module or module.startswith("."):
            stem = base.joinpath(module)
        else:
            stem = base.joinpath(*module.split("."))
    else:
        stem = PurePosixPath(*module.split("."))
    stem = PurePosixPath(posixpath.normpath(str(stem)))
    candidates = [str(stem) + ext for ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".json")]
    candidates += [str(stem / "__init__.py"), str(stem / "index.js"), str(stem / "index.ts")]
    for candidate in candidates:
        if candidate in file_paths:
            return candidate
    return None


def _python_file(path: str, text: str, file_paths: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    warnings: list[str] = []
    file_id = f"file:{path}"
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        warnings.append(f"python_syntax:{path}:{exc.lineno or 1}")
        return nodes, edges, warnings
    symbols: dict[tuple[str, int], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}

    def visit(body: list[ast.stmt], parent: dict[str, Any] | None = None) -> None:
        for item in body:
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(item, ast.ClassDef) else "function"
                line = int(getattr(item, "lineno", 1))
                end = int(getattr(item, "end_lineno", line) or line)
                n = _node(kind, path, item.name, line, end, "python", _snippet(text, line, min(end, line + 3)), "syntax")
                nodes.append(n)
                symbols[(item.name, line)] = n
                by_name.setdefault(item.name, []).append(n)
                edges.append(_edge(parent["id"] if parent else file_id, n["id"], "contains", path, line))
                visit(item.body, n)
            elif isinstance(item, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
                for field in ("body", "orelse", "finalbody"):
                    child = getattr(item, field, None)
                    if isinstance(child, list):
                        visit(child, parent)
                    elif child:
                        visit([child], parent)

    visit(tree.body)
    shadowed_names = {
        arg.arg
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for arg in (
            list(getattr(fn, "args", ()).posonlyargs)
            + list(getattr(fn, "args", ()).args)
            + list(getattr(fn, "args", ()).kwonlyargs)
            + ([getattr(fn, "args", ()).vararg] if getattr(fn, "args", ()).vararg else [])
            + ([getattr(fn, "args", ()).kwarg] if getattr(fn, "args", ()).kwarg else [])
        )
    }
    shadowed_names.update(
        alias.asname or alias.name.split(".")[0]
        for item in tree.body
        if isinstance(item, ast.Import)
        for alias in item.names
    )
    shadowed_names.update(
        alias.asname or alias.name
        for item in tree.body
        if isinstance(item, ast.ImportFrom)
        for alias in item.names
    )
    shadowed_names.update(n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            line = int(getattr(item, "lineno", 1))
            for alias in item.names:
                target = _module_target(path, alias.name, file_paths)
                if target:
                    edges.append(_edge(file_id, f"file:{target}", "imports", path, line))
        elif isinstance(item, ast.ImportFrom):
            line = int(getattr(item, "lineno", 1))
            module = "." * int(item.level or 0) + (item.module or "")
            target = _module_target(path, (item.module or ""), file_paths, relative=int(item.level or 0)) if item.level else _module_target(path, (item.module or ""), file_paths)
            if target:
                edges.append(_edge(file_id, f"file:{target}", "imports", path, line))
        elif isinstance(item, ast.Call):
            line = int(getattr(item, "lineno", 1))
            # Attribute calls (obj.foo()) are deliberately unresolved: a name
            # match alone cannot establish which object or imported module it
            # refers to.  Calls shadowed by an argument/local assignment are
            # likewise omitted rather than promoted to syntax facts.
            if not isinstance(item.func, ast.Name):
                continue
            name = item.func.id
            if not name or name not in by_name or len(by_name[name]) != 1 or name in shadowed_names:
                continue
            owner = file_id
            # Pick the innermost symbol enclosing this call.
            candidates = [n for n in nodes if n["line"] <= line <= n["end_line"]]
            if candidates:
                owner = sorted(candidates, key=lambda n: (n["end_line"] - n["line"], n["line"]))[0]["id"]
            # A call target is an inferred reference even when the callee name
            # is local and unambiguous; it is never runtime proof.
            edges.append(_edge(owner, by_name[name][0]["id"], "calls", path, line, "heuristic"))
    return nodes, edges, warnings


_JS_IMPORT = re.compile(r"\bimport\s+(?:[^;\n]*?\s+from\s+)?[\"']([^\"']+)[\"']|\brequire\(\s*[\"']([^\"']+)[\"']\s*\)")
_JS_CLASS = re.compile(r"\bclass\s+(" + _IDENT + r")")
_JS_FUNCTION = re.compile(r"\b(?:async\s+)?function\s*\*?\s*(" + _IDENT + r")\s*\(")
_JS_ARROW = re.compile(r"\b(?:const|let|var)\s+(" + _IDENT + r")\s*=\s*(?:async\s*)?(?:\([^\n]*\)|" + _IDENT + r")\s*=>")


def _js_file(path: str, text: str, file_paths: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    warnings: list[str] = []
    file_id = f"file:{path}"
    lines = text.splitlines()
    symbols: dict[str, dict[str, Any]] = {}
    for number, line_text in enumerate(lines, 1):
        for rx, kind in ((_JS_CLASS, "class"), (_JS_FUNCTION, "function"), (_JS_ARROW, "function")):
            for match in rx.finditer(line_text):
                name = match.group(1)
                if name in symbols:
                    continue
                n = _node(kind, path, name, number, number, "typescript" if path.lower().endswith((".ts", ".tsx")) else "javascript",
                          scrub(line_text.strip())[:MAX_SNIPPET], "heuristic")
                nodes.append(n)
                symbols[name] = n
                edges.append(_edge(file_id, n["id"], "contains", path, number, "heuristic"))
    for number, line_text in enumerate(lines, 1):
        for match in _JS_IMPORT.finditer(line_text):
            module = match.group(1) or match.group(2) or ""
            if module.startswith("."):
                target = _module_target(path, module, file_paths, relative=True)
                if target:
                    edges.append(_edge(file_id, f"file:{target}", "imports", path, number, "heuristic"))
        for call in re.finditer(r"\b(" + _IDENT + r")\s*\(", line_text):
            name = call.group(1)
            if name in {"if", "for", "while", "switch", "catch", "function", "import", "require"} or name not in symbols:
                continue
            owner = file_id
            enclosing = [n for n in nodes if n["line"] <= number <= n["end_line"]]
            if enclosing:
                owner = enclosing[0]["id"]
            edges.append(_edge(owner, symbols[name]["id"], "calls", path, number, "heuristic"))
    return nodes, edges, warnings


def _dedup_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result = []
    for edge in sorted(edges, key=lambda e: (e["source"], e["target"], e["kind"], e["path"], e["line"])):
        key = tuple(edge[k] for k in ("source", "target", "kind", "path", "line"))
        if key not in seen:
            seen.add(key)
            result.append(edge)
    return result


def build_snapshot(project: dict[str, Any]) -> dict[str, Any]:
    commit_sha = baseline_sha(project)
    root = _project_root(project)
    raw_records, _tree_count, tree_skipped = _tree_records(root, commit_sha)
    warnings: list[str] = []
    skipped: dict[str, int] = dict(tree_skipped)
    if "file_limit" in skipped:
        warnings.append("file_limit:超过 500 个可索引文件，仅索引排序靠前的 500 个")
    index_deadline = time.monotonic() + INDEX_TIMEOUT

    file_paths = {p for mode, obj_type, sha, p in raw_records if obj_type == "blob" and mode not in ("120000", "160000") and _allowed_path(p)}
    blobs: list[tuple[str, str]] = []
    source_bytes = 0
    for mode, obj_type, sha, path in raw_records:
        if time.monotonic() > index_deadline:
            raise ValueError("代码索引超过总时间预算")
        size_result = _git_run(root, ["cat-file", "-s", sha])
        try:
            size = int(size_result.stdout.decode("ascii", "strict").strip())
        except (ValueError, UnicodeDecodeError):
            skipped["blob_error"] = skipped.get("blob_error", 0) + 1
            continue
        if size > MAX_FILE_BYTES:
            skipped["file_bytes"] = skipped.get("file_bytes", 0) + 1
            continue
        if source_bytes + size > MAX_SOURCE_BYTES:
            skipped["source_bytes"] = skipped.get("source_bytes", 0) + 1
            continue
        source_bytes += size
        blobs.append((path, sha))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    file_node_paths: set[str] = set()
    for path, sha in sorted(blobs):
        if time.monotonic() > index_deadline:
            raise ValueError("代码索引超过总时间预算")
        result = _git_run(root, ["cat-file", "blob", sha])
        if result.returncode:
            skipped["blob_error"] = skipped.get("blob_error", 0) + 1
            continue
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            skipped["binary"] = skipped.get("binary", 0) + 1
            continue
        language = "python" if path.lower().endswith(".py") else "markdown" if path.lower().endswith(".md") else "json" if path.lower().endswith(".json") else "typescript" if path.lower().endswith((".ts", ".tsx")) else "javascript"
        # Public Markdown is documentation and needs a bounded text locator
        # for Chinese/document searches.  Source files retain only symbol
        # snippets; neither case stores the complete blob.
        file_snippet = scrub(text)[:MAX_SNIPPET] if language == "markdown" else ""
        file_node = _node("file", path, path, 1, max(1, len(text.splitlines())), language, file_snippet, "syntax")
        if len(nodes) >= MAX_NODES:
            skipped["node_limit"] = skipped.get("node_limit", 0) + 1
            continue
        nodes.append(file_node)
        node_ids.add(file_node["id"])
        file_node_paths.add(path)
        if path.lower().endswith(".py"):
            child_nodes, child_edges, child_warnings = _python_file(path, text, file_paths)
        elif Path(path).suffix.lower() in _JS_EXTS:
            child_nodes, child_edges, child_warnings = _js_file(path, text, file_paths)
        else:
            child_nodes, child_edges, child_warnings = [], [], []
        for child in child_nodes:
            if len(nodes) >= MAX_NODES:
                skipped["node_limit"] = skipped.get("node_limit", 0) + 1
                break
            nodes.append(child)
            node_ids.add(child["id"])
        warnings.extend(child_warnings)
        edges.extend(child_edges)

    valid_edges = [e for e in _dedup_edges(edges) if e["source"] in node_ids and e["target"] in node_ids]
    if len(valid_edges) > MAX_EDGES:
        skipped["edge_limit"] = len(valid_edges) - MAX_EDGES
        warnings.append("edge_limit:超过 10000 条边，已截断")
        valid_edges = valid_edges[:MAX_EDGES]
    if skipped:
        warnings.extend(f"skipped_{key}:{value}" for key, value in sorted(skipped.items()))
    nodes.sort(key=lambda n: n["id"])
    stats = {"files": len(file_node_paths), "files_indexed": len(file_node_paths),
             "files_skipped": sum(skipped.values()), "source_bytes": source_bytes,
             "nodes": len(nodes), "edges": len(valid_edges), "skipped": dict(sorted(skipped.items()))}
    return {"schema_version": SCHEMA_VERSION, "project_id": project["id"], "commit_sha": commit_sha,
            "parser_version": PARSER_VERSION, "indexed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "nodes": nodes, "edges": valid_edges, "warnings": sorted(set(warnings)), "stats": stats}


def _ensure_snapshot_table(store: Any) -> None:
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS code_snapshots(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, commit_sha TEXT NOT NULL,
            parser_version TEXT NOT NULL, indexed_at TEXT NOT NULL, data TEXT NOT NULL,
            UNIQUE(project_id, commit_sha, parser_version))""")
        db.executescript("""
            CREATE TRIGGER IF NOT EXISTS no_code_snapshot_update
            BEFORE UPDATE ON code_snapshots
            BEGIN SELECT RAISE(ABORT, 'code snapshots are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS no_code_snapshot_delete
            BEFORE DELETE ON code_snapshots
            BEGIN SELECT RAISE(ABORT, 'code snapshots are append-only'); END;
        """)


def save_snapshot(store: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    store.project(snapshot["project_id"])
    _ensure_snapshot_table(store)
    clean = json.loads(json.dumps(snapshot, ensure_ascii=False))
    clean.pop("id", None)
    clean = scrub(clean)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id,data FROM code_snapshots WHERE project_id=? AND commit_sha=? AND parser_version=?",
                         (clean["project_id"], clean["commit_sha"], clean["parser_version"])).fetchone()
        if row is None:
            ident = uuid.uuid4().hex
            db.execute("INSERT INTO code_snapshots VALUES (?,?,?,?,?,?)",
                       (ident, clean["project_id"], clean["commit_sha"], clean["parser_version"], clean["indexed_at"], json.dumps(clean, ensure_ascii=False)))
            row = db.execute("SELECT id,data FROM code_snapshots WHERE id=?", (ident,)).fetchone()
        saved = json.loads(row[1])
        saved["id"] = row[0]
        return saved


def get_snapshot(store: Any, pid: str, commit_sha: str | None = None) -> dict[str, Any] | None:
    project = store.project(pid)
    _ensure_snapshot_table(store)
    with store.connect() as db:
        target_sha = commit_sha or baseline_sha(project)
        row = db.execute("SELECT id,data FROM code_snapshots WHERE project_id=? AND commit_sha=? AND parser_version=? ORDER BY rowid DESC LIMIT 1",
                         (pid, target_sha, PARSER_VERSION)).fetchone()
        if row is None:
            # A stale snapshot is useful to callers that need to report the
            # mismatch, so preserve the latest fallback when the exact current
            # baseline has not been indexed yet.
            row = db.execute("SELECT id,data FROM code_snapshots WHERE project_id=? ORDER BY rowid DESC LIMIT 1", (pid,)).fetchone()
    if row is None:
        return None
    data = json.loads(row[1])
    data["id"] = row[0]
    return data


def search_snapshot(snapshot: dict[str, Any], query: str, limit: int = 10) -> list[dict[str, Any]]:
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("搜索条件过长")
    if not isinstance(limit, int) or not 1 <= limit <= 30:
        raise ValueError("搜索数量必须在 1 到 30 之间")
    if not query.strip():
        return []
    q = query.casefold()
    tokens = [t.casefold() for t in re.findall(r"[\w]+", query, flags=re.UNICODE)] or [q]
    scores: dict[str, float] = {}
    for node in snapshot.get("nodes", []):
        name = str(node.get("name", "")); path = str(node.get("path", "")); snippet = str(node.get("snippet", ""))
        score = 0.0
        for token in tokens:
            if token == name.casefold(): score += 12
            elif token in name.casefold(): score += 8
            if token in path.casefold(): score += 4
            if token in snippet.casefold(): score += 2
        if q in name.casefold(): score += 3
        if q in path.casefold(): score += 2
        if score:
            scores[node["id"]] = score
    by_id = {n["id"]: n for n in snapshot.get("nodes", [])}
    base_scores = dict(scores)
    for edge in snapshot.get("edges", []):
        if edge.get("source") in base_scores and edge.get("target") in by_id:
            scores[edge["target"]] = scores.get(edge["target"], 0) + 1
        if edge.get("target") in base_scores and edge.get("source") in by_id:
            scores[edge["source"]] = scores.get(edge["source"], 0) + 1
    result = []
    for ident, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        n = by_id[ident]
        result.append({"node_id": ident, "path": n["path"], "name": n["name"], "kind": n["kind"],
                       "line": n["line"], "end_line": n["end_line"], "score": score,
                       "snippet": n.get("snippet", ""), "resolution": n.get("resolution", "syntax")})
    return result


def graph_slice(snapshot: dict[str, Any], node_id: str | None = None, limit: int = 80) -> dict[str, Any]:
    if not isinstance(limit, int) or not 1 <= limit <= 80:
        raise ValueError("图切片数量必须在 1 到 80 之间")
    nodes = list(snapshot.get("nodes", [])); by_id = {n["id"]: n for n in nodes}
    if node_id is not None and node_id not in by_id:
        raise KeyError(node_id)
    edges = [e for e in snapshot.get("edges", []) if e.get("source") in by_id and e.get("target") in by_id]
    if node_id is None:
        ordered = sorted(nodes, key=lambda n: (0 if n["kind"] == "file" else 1, n["id"]))
    else:
        neighbor_ids = {node_id}
        for edge in edges:
            if edge["source"] == node_id: neighbor_ids.add(edge["target"])
            elif edge["target"] == node_id: neighbor_ids.add(edge["source"])
        ordered = [by_id[node_id]] + sorted((by_id[i] for i in neighbor_ids if i != node_id), key=lambda n: n["id"])
    truncated = len(ordered) > limit
    selected = {n["id"] for n in ordered[:limit]}
    out_nodes = sorted((by_id[i] for i in selected), key=lambda n: n["id"])
    out_edges = sorted((e for e in edges if e["source"] in selected and e["target"] in selected),
                       key=lambda e: (e["source"], e["target"], e["kind"], e["path"], e["line"]))
    if len(out_edges) > MAX_EDGES:
        out_edges = out_edges[:MAX_EDGES]; truncated = True
    return {"nodes": out_nodes, "edges": out_edges, "truncated": truncated}
