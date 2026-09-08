"""Durable, scoped project knowledge.

This module deliberately treats imported markdown and merge observations as data.  It
does not read repositories, execute YAML, or call a model.  The small current-pointer
tables make the immutable version tables useful for CAS updates while keeping every
change auditable in the same SQLite transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlparse

from factory.control.store import Conflict, now, scrub


_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_BRANCH = re.compile(r"^[^\x00\r\n]{1,200}$")
_SECRET_FILE = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|\.npmrc|\.netrc|(?:secret|credential|password|token)(?:[._-].*)?|id_rsa(?:\..*)?|.*\.(?:pem|key|crt|cer|p12|pfx|kdbx))$"
)
_PATH_BAD = re.compile(r"[\x00\r\n*?\[\]{}]")
_KINDS = {"fact", "decision", "hypothesis"}
_STATUSES = {"candidate", "active", "retired"}
_IMPORT_SOURCE = "teamai_import"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_int(value, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _text(value, name: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if required and not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} is too long")
    return scrub(value)


def _actor(value) -> str:
    return _text(value, "actor", 200)


def _safe_path(value: str, name: str = "path") -> str:
    value = _text(value, name, 1000)
    if not value or value.startswith("/") or "\\" in value or _PATH_BAD.search(value):
        raise ValueError(f"{name} must be a safe repository-relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{name} must be a concrete repository-relative path")
    if ".git" in {p.casefold() for p in parts}:
        raise ValueError(f"{name} may not refer to .git")
    if any(_SECRET_FILE.match(part) for part in parts):
        raise ValueError(f"{name} may not refer to a secret or configuration file")
    return value


def _paths(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("paths must be a list of at most 20 paths")
    result = []
    seen = set()
    for item in value:
        path = _safe_path(item, "path")
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _sha(value, name: str, *, length: int = 40, required: bool = True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not (len(value) == length and re.fullmatch(r"[0-9a-fA-F]+", value)):
        raise ValueError(f"{name} must be a {length}-character hexadecimal SHA")
    return value.lower()


def _mapping(value, name: str):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


class KnowledgeStore:
    """Project-scoped knowledge backed by the existing :class:`Store` database."""

    def __init__(self, store):
        self.store = store
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_agents(
                    project_id TEXT PRIMARY KEY, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_agent_versions(
                    project_id TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    name TEXT NOT NULL, mission TEXT NOT NULL, architecture_summary TEXT NOT NULL,
                    constraints TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, revision)
                );
                CREATE TABLE IF NOT EXISTS knowledge_entries(
                    project_id TEXT NOT NULL, key TEXT NOT NULL, id TEXT NOT NULL,
                    revision INTEGER NOT NULL, PRIMARY KEY(project_id, key)
                );
                CREATE TABLE IF NOT EXISTS knowledge_entry_versions(
                    project_id TEXT NOT NULL, key TEXT NOT NULL, id TEXT NOT NULL,
                    revision INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                    title TEXT NOT NULL, content TEXT NOT NULL, paths TEXT NOT NULL,
                    commit_sha TEXT, provenance TEXT NOT NULL, actor TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(project_id, key, revision)
                );
                CREATE TABLE IF NOT EXISTS knowledge_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                    type TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_previews(
                    project_id TEXT NOT NULL, id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                    repository TEXT NOT NULL, documents TEXT NOT NULL, warnings TEXT NOT NULL,
                    actor TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_import_applies(
                    project_id TEXT NOT NULL, preview_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                    indices TEXT NOT NULL, entry_keys TEXT NOT NULL, actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_merge_ledger(
                    project_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    entry_key TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, idempotency_key)
                );
                CREATE TRIGGER IF NOT EXISTS no_knowledge_agent_version_update
                    BEFORE UPDATE ON knowledge_agent_versions BEGIN
                    SELECT RAISE(ABORT,'knowledge agent versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_agent_version_delete
                    BEFORE DELETE ON knowledge_agent_versions BEGIN
                    SELECT RAISE(ABORT,'knowledge agent versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_entry_version_update
                    BEFORE UPDATE ON knowledge_entry_versions BEGIN
                    SELECT RAISE(ABORT,'knowledge entry versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_entry_version_delete
                    BEFORE DELETE ON knowledge_entry_versions BEGIN
                    SELECT RAISE(ABORT,'knowledge entry versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_audit_update
                    BEFORE UPDATE ON knowledge_audit BEGIN
                    SELECT RAISE(ABORT,'knowledge audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_audit_delete
                    BEFORE DELETE ON knowledge_audit BEGIN
                    SELECT RAISE(ABORT,'knowledge audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_preview_update
                    BEFORE UPDATE ON knowledge_previews BEGIN
                    SELECT RAISE(ABORT,'knowledge previews are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_preview_delete
                    BEFORE DELETE ON knowledge_previews BEGIN
                    SELECT RAISE(ABORT,'knowledge previews are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_import_apply_update
                    BEFORE UPDATE ON knowledge_import_applies BEGIN
                    SELECT RAISE(ABORT,'knowledge import ledger is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_import_apply_delete
                    BEFORE DELETE ON knowledge_import_applies BEGIN
                    SELECT RAISE(ABORT,'knowledge import ledger is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_merge_ledger_update
                    BEFORE UPDATE ON knowledge_merge_ledger BEGIN
                    SELECT RAISE(ABORT,'knowledge merge ledger is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_knowledge_merge_ledger_delete
                    BEFORE DELETE ON knowledge_merge_ledger BEGIN
                    SELECT RAISE(ABORT,'knowledge merge ledger is append-only'); END;
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(knowledge_agents)")}
            if "created_at" not in columns:
                at = now()
                db.execute("ALTER TABLE knowledge_agents ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE knowledge_agents ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE knowledge_agents SET created_at=?,updated_at=? WHERE created_at=''", (at, at))

    def _project(self, pid):
        if not isinstance(pid, str) or not pid:
            raise ValueError("project_id must be a non-empty string")
        return self.store.project(pid)

    @staticmethod
    def _audit(db, pid, kind, payload):
        db.execute(
            "INSERT INTO knowledge_audit(project_id,type,payload,at) VALUES (?,?,?,?)",
            (pid, kind, _json(scrub(payload)), now()),
        )

    @staticmethod
    def _agent_row(row):
        return {
            "id": row["id"], "project_id": row["project_id"], "revision": row["revision"],
            "name": row["name"], "mission": row["mission"],
            "architecture_summary": row["architecture_summary"],
            "constraints": json.loads(row["constraints"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _entry_row(row):
        return {
            "id": row["id"], "key": row["key"], "project_id": row["project_id"],
            "revision": row["revision"], "kind": row["kind"], "status": row["status"],
            "title": row["title"], "content": row["content"], "paths": json.loads(row["paths"]),
            "commit_sha": row["commit_sha"], "provenance": json.loads(row["provenance"]),
            "actor": row["actor"], "created_at": row["created_at"],
        }

    def agent(self, pid):
        project = self._project(pid)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM knowledge_agents WHERE project_id=?", (pid,)).fetchone()
            if current is None:
                aid, at = uuid.uuid4().hex, now()
                db.execute("INSERT INTO knowledge_agents VALUES (?,?,?,?,?)", (pid, aid, 1, at, at))
                db.execute(
                    "INSERT INTO knowledge_agent_versions VALUES (?,?,?,?,?,?,?,?)",
                    (pid, aid, 1, _text(project.get("name", ""), "name", 120, required=False), "", "", "[]", at),
                )
                self._audit(db, pid, "knowledge.agent.created", {"agent_id": aid, "revision": 1})
                current = db.execute(
                    "SELECT a.project_id,a.id,a.revision,a.created_at,a.updated_at,v.name,v.mission,v.architecture_summary,v.constraints,v.created_at AS version_at "
                    "FROM knowledge_agents a JOIN knowledge_agent_versions v ON v.project_id=a.project_id AND v.revision=a.revision "
                    "WHERE a.project_id=?", (pid,)
                ).fetchone()
            else:
                current = db.execute(
                    "SELECT a.project_id,a.id,a.revision,a.created_at,a.updated_at,v.name,v.mission,v.architecture_summary,v.constraints,v.created_at AS version_at "
                    "FROM knowledge_agents a JOIN knowledge_agent_versions v ON v.project_id=a.project_id AND v.revision=a.revision "
                    "WHERE a.project_id=?", (pid,)
                ).fetchone()
            return self._agent_row(current)

    def update_agent(self, pid, data, expected_revision, actor):
        self._project(pid)
        data = _mapping(data, "data")
        _strict_int(expected_revision, "expected_revision", minimum=1)
        actor = _actor(actor)
        allowed = {"name", "mission", "architecture_summary", "constraints"}
        if set(data) - allowed:
            raise ValueError("unknown agent fields")
        name = _text(data.get("name", ""), "name", 120, required=False)
        mission = _text(data.get("mission", ""), "mission", 2000, required=False)
        architecture = _text(data.get("architecture_summary", ""), "architecture_summary", 4000, required=False)
        constraints = data.get("constraints", [])
        if not isinstance(constraints, list) or len(constraints) > 20:
            raise ValueError("constraints must be a list of at most 20 strings")
        constraints = [_text(x, "constraint", 300) for x in constraints]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM knowledge_agents WHERE project_id=?", (pid,)).fetchone()
            if current is None:
                # Calling agent() outside this transaction would create a race window.
                project = self.store.project(pid)
                aid, at = uuid.uuid4().hex, now()
                db.execute("INSERT INTO knowledge_agents VALUES (?,?,?,?,?)", (pid, aid, 1, at, at))
                db.execute("INSERT INTO knowledge_agent_versions VALUES (?,?,?,?,?,?,?,?)",
                           (pid, aid, 1, _text(project.get("name", ""), "name", 120, required=False), "", "", "[]", at))
                self._audit(db, pid, "knowledge.agent.created", {"agent_id": aid, "revision": 1})
                current = db.execute("SELECT * FROM knowledge_agents WHERE project_id=?", (pid,)).fetchone()
            if current["revision"] != expected_revision:
                raise Conflict("agent profile has changed; reload before updating")
            prior = db.execute("SELECT * FROM knowledge_agent_versions WHERE project_id=? AND revision=?",
                               (pid, current["revision"])).fetchone()
            if "name" not in data:
                name = prior["name"]
            if "mission" not in data:
                mission = prior["mission"]
            if "architecture_summary" not in data:
                architecture = prior["architecture_summary"]
            if "constraints" not in data:
                constraints = json.loads(prior["constraints"])
            revision, at = current["revision"] + 1, now()
            db.execute("INSERT INTO knowledge_agent_versions VALUES (?,?,?,?,?,?,?,?)",
                       (pid, current["id"], revision, name, mission, architecture, _json(constraints), at))
            db.execute("UPDATE knowledge_agents SET revision=?,updated_at=? WHERE project_id=?", (revision, at, pid))
            self._audit(db, pid, "knowledge.agent.updated", {"agent_id": current["id"], "revision": revision, "actor": actor})
            row = db.execute(
                "SELECT a.project_id,a.id,a.revision,a.created_at,a.updated_at,v.name,v.mission,v.architecture_summary,v.constraints,v.created_at AS version_at "
                "FROM knowledge_agents a JOIN knowledge_agent_versions v ON v.project_id=a.project_id AND v.revision=a.revision "
                "WHERE a.project_id=?", (pid,)
            ).fetchone()
            # Retain the variable to make it obvious that prior is intentionally read only.
            del prior
            return self._agent_row(row)

    def _normal_entry_data(self, data):
        data = _mapping(data, "data")
        allowed = {"kind", "status", "title", "content", "paths", "commit_sha"}
        if set(data) - allowed:
            raise ValueError("unknown knowledge entry fields")
        kind = data.get("kind", "hypothesis")
        status = data.get("status", "candidate")
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError("invalid knowledge entry kind")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("invalid knowledge entry status")
        return {
            "kind": kind, "status": status,
            "title": _text(data.get("title", ""), "title", 200),
            "content": _text(data.get("content", ""), "content", 8000),
            "paths": _paths(data.get("paths", [])),
            "commit_sha": _sha(data.get("commit_sha"), "commit_sha", required=False),
        }

    @staticmethod
    def _provenance(existing, incoming, actor, *, status):
        if existing:
            origin = dict(existing)
            if status == "active":
                origin["reviewer"] = actor
                origin["approved_at"] = now()
            return scrub(origin)
        # Provenance is never accepted as an authority claim from a request.  A new
        # human entry gets a server-generated origin; imported entries are created only
        # by apply_import, which supplies its own server-generated origin.
        if incoming is not None:
            raise ValueError("provenance is server generated")
        return {"source": "human", "actor": actor}

    def _insert_entry(self, db, pid, key, entry_id, revision, values, actor, provenance):
        count = db.execute("SELECT COUNT(*) FROM knowledge_entries WHERE project_id=?", (pid,)).fetchone()[0]
        # New resources are identified by revision 1 by all callers.  Do not use
        # ``key is None`` here: import and merge paths allocate their key before
        # calling this helper, but must obey the same project-wide cap.
        if revision == 1 and count >= 1000:
            raise ValueError("project knowledge is limited to 1000 entries")
        if revision > 100:
            raise ValueError("knowledge entry history is limited to 100 versions")
        at = now()
        db.execute(
            "INSERT INTO knowledge_entry_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, key, entry_id, revision, values["kind"], values["status"], values["title"], values["content"],
             _json(values["paths"]), values["commit_sha"], _json(provenance), actor, at),
        )
        if revision == 1:
            db.execute("INSERT INTO knowledge_entries VALUES (?,?,?,?)", (pid, key, entry_id, revision))
        else:
            db.execute("UPDATE knowledge_entries SET revision=? WHERE project_id=? AND key=?", (revision, pid, key))
        return self._entry_row(db.execute("SELECT * FROM knowledge_entry_versions WHERE project_id=? AND key=? AND revision=?",
                                          (pid, key, revision)).fetchone())

    def put_entry(self, pid, data, actor, key=None, expected_revision=0, provenance=None):
        self._project(pid)
        actor = _actor(actor)
        values = self._normal_entry_data(data)
        _strict_int(expected_revision, "expected_revision", minimum=0)
        if key is not None and (not isinstance(key, str) or not key):
            raise ValueError("key must be a non-empty string")
        if key is None and expected_revision != 0:
            raise ValueError("new entries require expected_revision=0")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM knowledge_entries WHERE project_id=? AND key=?", (pid, key)).fetchone() if key else None
            if key and current is None:
                raise KeyError(key)
            if current is None:
                key, entry_id, revision = uuid.uuid4().hex, uuid.uuid4().hex, 1
                origin = self._provenance(None, provenance, actor, status=values["status"])
                entry = self._insert_entry(db, pid, key, entry_id, revision, values, actor, origin)
                self._audit(db, pid, "knowledge.entry.created", {"key": key, "revision": revision, "actor": actor})
                return entry
            if current["revision"] != expected_revision:
                raise Conflict("knowledge entry has changed; reload before updating")
            prior = db.execute("SELECT * FROM knowledge_entry_versions WHERE project_id=? AND key=? AND revision=?",
                               (pid, key, current["revision"])).fetchone()
            prior_origin = json.loads(prior["provenance"])
            if provenance is not None and scrub(provenance) != prior_origin:
                raise ValueError("provenance may not be replaced")
            origin = self._provenance(prior_origin, None, actor, status=values["status"])
            entry = self._insert_entry(db, pid, key, current["id"], current["revision"] + 1, values, actor, origin)
            self._audit(db, pid, "knowledge.entry.updated", {"key": key, "revision": entry["revision"], "actor": actor})
            return entry

    def entries(self, pid, include_retired=False):
        self._project(pid)
        if not isinstance(include_retired, bool):
            raise ValueError("include_retired must be boolean")
        with self.store.connect() as db:
            condition = "" if include_retired else " AND v.status != 'retired'"
            rows = db.execute(
                "SELECT v.* FROM knowledge_entries e JOIN knowledge_entry_versions v "
                "ON v.project_id=e.project_id AND v.key=e.key AND v.revision=e.revision "
                f"WHERE e.project_id=?{condition} ORDER BY v.created_at, v.key LIMIT 1001", (pid,)
            ).fetchall()
            if len(rows) > 1000:
                raise ValueError("project knowledge exceeds the 1000 entry limit")
            return [self._entry_row(r) for r in rows]

    def versions(self, pid, key):
        self._project(pid)
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM knowledge_entries WHERE project_id=? AND key=?", (pid, key)).fetchone()
            if exists is None:
                raise KeyError(key)
            rows = db.execute("SELECT * FROM knowledge_entry_versions WHERE project_id=? AND key=? ORDER BY revision LIMIT 101",
                              (pid, key)).fetchall()
            if len(rows) > 100:
                raise ValueError("knowledge entry history exceeds the 100 version limit")
            return [self._entry_row(r) for r in rows]

    @staticmethod
    def _normalize_checks(raw):
        """Keep merge evidence useful without persisting command output or blobs."""
        if not isinstance(raw, (dict, list, tuple)):
            raise ValueError("check_results must be a list or object")
        items = []
        source = raw.items() if isinstance(raw, dict) else enumerate(raw)
        for key, value in source:
            if isinstance(value, dict):
                name = value.get("name", key)
                result = value.get("exit", value.get("returncode", value.get("status", "unknown")))
            else:
                name, result = key, value
            name = _text(str(name), "check name", 120)
            if isinstance(result, (dict, list, tuple)):
                result = _json(scrub(result))
            result = _text(str(result), "check result", 240)
            items.append({"name": name, "result": result})
            if len(items) >= 100:
                break
        # Sorting makes retries and equivalent evidence produce the same summary.
        return sorted(items, key=lambda item: (item["name"], item["result"]))

    def preview_import(self, pid, bundle, actor):
        project = self._project(pid)
        bundle = _mapping(bundle, "bundle")
        if set(bundle) != {"repository", "documents"}:
            raise ValueError("bundle must contain repository and documents only")
        repository = bundle["repository"]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ValueError("invalid repository")
        if repository.casefold() != str(project.get("repository", "")).casefold():
            raise ValueError("bundle repository does not match project")
        actor = _actor(actor)
        documents = bundle["documents"]
        if not isinstance(documents, list) or not documents or len(documents) > 20:
            raise ValueError("documents must contain 1 to 20 markdown documents")
        out, seen, total = [], set(), 0
        warnings = []
        for index, document in enumerate(documents):
            document = _mapping(document, "document")
            if set(document) != {"path", "content"}:
                raise ValueError("document must contain path and content only")
            path = _safe_path(document["path"], "document path")
            if not path.casefold().endswith(".md"):
                raise ValueError("wiki imports must be markdown")
            folded = path.casefold()
            if folded in seen:
                raise ValueError("duplicate document path")
            seen.add(folded)
            raw_content = document["content"]
            if not isinstance(raw_content, str):
                raise ValueError("document content must be a string")
            if len(raw_content) > 16000:
                raise ValueError("document content is too long")
            raw_sha256 = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
            content = _text(raw_content, "document content", 16000, required=False)
            redacted_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            total += len(content)
            if total > 160000:
                raise ValueError("wiki import is too large")
            title = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ") and line[2:].strip()),
                         PurePosixPath(path).stem)
            title = title[:200]
            out.append({"index": index, "path": path, "title": title, "content": content,
                        "sha256": raw_sha256, "content_sha256": redacted_sha256})
            if len(content) > 8000:
                warnings.append(f"文档 {index + 1} 超过 8000 字符，将分段导入为候选知识")
        canonical = {"repository": repository, "documents": [{"path": x["path"], "content": x["content"]} for x in out]}
        digest = hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()
        created = now()
        preview_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO knowledge_previews VALUES (?,?,?,?,?,?,?,?)",
                       (pid, preview_id, digest, repository, _json(out), _json(warnings), actor, created))
            self._audit(db, pid, "knowledge.import.preview", {"preview_id": preview_id, "sha256": digest, "actor": actor})
        return {"id": preview_id, "project_id": pid, "sha256": digest, "repository": repository,
                "documents": out, "warnings": warnings, "created_at": created}

    def apply_import(self, pid, preview_id, sha256, indices, actor):
        self._project(pid)
        if not isinstance(preview_id, str) or not preview_id:
            raise ValueError("preview_id must be a non-empty string")
        _sha(sha256, "sha256", length=64)
        if not isinstance(indices, list) or not indices:
            raise ValueError("indices must be a non-empty list")
        if len(indices) > 20 or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in indices):
            raise ValueError("indices must contain unique non-negative integers")
        if len(set(indices)) != len(indices):
            raise ValueError("indices must be unique")
        indices = sorted(indices)
        actor = _actor(actor)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            preview = db.execute("SELECT * FROM knowledge_previews WHERE project_id=? AND id=?", (pid, preview_id)).fetchone()
            if preview is None:
                raise KeyError(preview_id)
            if preview["sha256"] != sha256.lower():
                raise Conflict("preview hash does not match")
            ledger = db.execute("SELECT * FROM knowledge_import_applies WHERE project_id=? AND preview_id=?", (pid, preview_id)).fetchone()
            if ledger is not None:
                if json.loads(ledger["indices"]) != indices:
                    raise Conflict("preview was already applied with a different selection")
                keys = json.loads(ledger["entry_keys"])
                rows = [db.execute("SELECT * FROM knowledge_entries e JOIN knowledge_entry_versions v ON v.project_id=e.project_id AND v.key=e.key AND v.revision=e.revision WHERE e.project_id=? AND e.key=?",
                                   (pid, key)).fetchone() for key in keys]
                return {"entries": [self._entry_row(row) for row in rows if row is not None], "duplicate": True}
            docs = json.loads(preview["documents"])
            if any(i >= len(docs) for i in indices):
                raise ValueError("selected document index is invalid")
            chunks = []
            for i in indices:
                doc = docs[i]
                content = doc["content"]
                doc_chunks = [content[offset:offset + 8000] for offset in range(0, len(content), 8000)] or [""]
                for chunk_index, chunk in enumerate(doc_chunks):
                    chunks.append((i, doc, chunk_index, len(doc_chunks), chunk))
            count = db.execute("SELECT COUNT(*) FROM knowledge_entries WHERE project_id=?", (pid,)).fetchone()[0]
            if count + len(chunks) > 1000:
                raise ValueError("project knowledge is limited to 1000 entries")
            keys, entries = [], []
            for i, doc, chunk_index, chunk_count, chunk in chunks:
                redacted_sha256 = doc.get("content_sha256") or hashlib.sha256(doc["content"].encode("utf-8")).hexdigest()
                values = {"kind": "hypothesis", "status": "candidate", "title": doc["title"],
                          "content": chunk, "paths": [doc["path"]], "commit_sha": None}
                origin = {"source": _IMPORT_SOURCE, "path": doc["path"], "original_path": doc["path"],
                          "original_index": i, "chunk_index": chunk_index, "chunk_count": chunk_count,
                          "sha256": redacted_sha256, "original_sha256": doc["sha256"],
                          "redacted_sha256": redacted_sha256, "repository": preview["repository"]}
                if chunk_count > 1:
                    values["title"] = f"{doc['title']} (part {chunk_index + 1}/{chunk_count})"[:200]
                key, entry_id = uuid.uuid4().hex, uuid.uuid4().hex
                entries.append(self._insert_entry(db, pid, key, entry_id, 1, values, actor, origin))
                keys.append(key)
            created = now()
            db.execute("INSERT INTO knowledge_import_applies VALUES (?,?,?,?,?,?,?)",
                       (pid, preview_id, sha256.lower(), _json(indices), _json(keys), actor, created))
            self._audit(db, pid, "knowledge.import.applied", {"preview_id": preview_id, "indices": indices, "actor": actor})
            return {"entries": entries, "duplicate": False}

    def record_merge(self, pid, run, evidence):
        project = self._project(pid)
        evidence = _mapping(evidence, "evidence")
        if evidence.get("merged") is False:
            raise ValueError("merge evidence must confirm a merged pull request")
        if isinstance(run, str):
            run = self.store.get(run)
        run = _mapping(run, "run")
        run_id = run.get("id", run.get("run_id"))
        run_id = _text(run_id, "run_id", 200)
        if run.get("project_id") is not None and run["project_id"] != pid:
            raise KeyError(pid)
        required = ("head_sha", "merge_commit_sha", "pr_url", "pr_number", "repository", "base_branch", "merged_at")
        if any(k not in evidence for k in required):
            raise ValueError("merge evidence is incomplete")
        head_sha = _sha(evidence["head_sha"], "head_sha")
        merge_sha = _sha(evidence["merge_commit_sha"], "merge_commit_sha")
        repository = evidence["repository"]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or repository.casefold() != str(project.get("repository", "")).casefold():
            raise ValueError("merge repository does not match project")
        pr_number = _strict_int(evidence["pr_number"], "pr_number", minimum=1)
        pr_url = _text(evidence["pr_url"], "pr_url", 1000)
        parsed = urlparse(pr_url)
        canonical = f"https://github.com/{repository}/pull/{pr_number}"
        if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com" or parsed.path.rstrip("/") != f"/{repository}/pull/{pr_number}" or parsed.query or parsed.fragment:
            raise ValueError("pr_url must be the canonical GitHub pull request URL")
        base_branch = evidence["base_branch"]
        if not isinstance(base_branch, str) or not _BRANCH.fullmatch(base_branch):
            raise ValueError("invalid base_branch")
        merged_at = _text(evidence["merged_at"], "merged_at", 100)
        paths = []
        all_path_count = 0
        omitted_path_count = 0
        plan = run.get("plan") or {}
        tasks = plan.get("tasks", []) if isinstance(plan, dict) else []
        if not isinstance(tasks, list):
            raise ValueError("run plan tasks must be a list")
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("run plan task must be an object")
            task_paths = task.get("paths", [])
            if not isinstance(task_paths, list):
                omitted_path_count += 1
                continue
            for raw_path in task_paths:
                all_path_count += 1
                try:
                    path = _safe_path(raw_path, "path")
                except ValueError:
                    omitted_path_count += 1
                    continue
                if path not in paths:
                    if len(paths) < 20:
                        paths.append(path)
                    else:
                        omitted_path_count += 1
        checks_source = "merge_evidence"
        if "check_results" in evidence:
            checks = evidence["check_results"]
        elif "checks" in evidence:
            checks = evidence["checks"]
        else:
            # These are checks executed by the run at its head, not checks rerun
            # against the subsequently merged commit.  Preserve that distinction
            # in both the factual text and provenance.
            artifacts = run.get("artifacts")
            checks = artifacts.get("checks", []) if isinstance(artifacts, dict) else []
            checks_source = "run.artifacts.checks"
        normalized_checks = self._normalize_checks(checks)
        check_text = ", ".join(f"{item['name']}={item['result']}" for item in normalized_checks)
        idempotency = f"{repository.casefold()}:{pr_number}:{merge_sha}"
        origin = {"source": "github_merge", "run_id": run_id, "repository": repository,
                  "pr_number": pr_number, "pr_url": canonical, "head_sha": head_sha,
                  "merge_commit_sha": merge_sha, "base_branch": base_branch, "merged_at": merged_at,
                  "check_results": normalized_checks, "check_results_source": checks_source,
                  "path_count": all_path_count, "omitted_path_count": omitted_path_count}
        stored_evidence = scrub(origin)
        content = f"Verified run {run_id} merged commit {merge_sha} from pull request #{pr_number}."
        if check_text:
            content += f" Executed-head checks (not rerun at merge): {check_text}."
        values = {"kind": "fact", "status": "active", "title": f"Merged run {run_id}",
                  "content": content[:8000], "paths": paths, "commit_sha": merge_sha}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM knowledge_merge_ledger WHERE project_id=? AND idempotency_key=?",
                               (pid, idempotency)).fetchone()
            if prior is not None:
                row = db.execute("SELECT v.* FROM knowledge_entries e JOIN knowledge_entry_versions v ON v.project_id=e.project_id AND v.key=e.key AND v.revision=e.revision WHERE e.project_id=? AND e.key=?",
                                 (pid, prior["entry_key"])).fetchone()
                if row is None:
                    raise ValueError("merge ledger points to missing knowledge entry")
                return {"entry": self._entry_row(row), "duplicate": True}
            key, entry_id = uuid.uuid4().hex, uuid.uuid4().hex
            entry = self._insert_entry(db, pid, key, entry_id, 1, values, "github-verified", origin)
            created = now()
            db.execute("INSERT INTO knowledge_merge_ledger VALUES (?,?,?,?,?)",
                       (pid, idempotency, key, _json(stored_evidence), created))
            self._audit(db, pid, "knowledge.merge.recorded", {"key": key, "run_id": run_id,
                                                               "merge_commit_sha": merge_sha})
            return {"entry": entry, "duplicate": False}

    def audit(self, pid, after=0, limit=100):
        self._project(pid)
        _strict_int(after, "after", minimum=0)
        _strict_int(limit, "limit", minimum=1)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM knowledge_audit WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
                              (pid, after, min(limit, 100))).fetchall()
            return [{"id": row["id"], "type": row["type"], "payload": json.loads(row["payload"]), "at": row["at"]}
                    for row in rows]
