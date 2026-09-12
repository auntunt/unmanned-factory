"""Durable, validated storage for vertical agents and safe Skill assets."""
from __future__ import annotations

import hashlib
import io
from contextlib import nullcontext
import json
import re
import unicodedata
import uuid
import zipfile
from typing import Any, Mapping

from factory.control.store import Conflict, now, scrub

MAX_ZIP = 20 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_FILES = 500
MAX_FILE = 2 * 1024 * 1024
KNOWN_PROVIDERS = frozenset(("codex", "claude", "dsh"))
MODEL_STAGES = frozenset(("default", "planning", "analysis", "execution", "verification", "maintenance"))
MODEL_PARAMETERS = frozenset(("temperature", "top_p", "max_tokens", "reasoning_effort"))
PATCH_FIELDS = frozenset(("instructions", "model_settings", "tool_scope", "acceptance", "delivery", "skill_ids"))

# A provider session is an optimization, not the source of project truth.  If
# Claude has already reread a large conversation without a single prompt-cache
# hit, carrying that transcript into the next feedback run makes every tool
# turn slower and more expensive.  Start from the verified commit and bounded
# owner history instead.  The threshold is deliberately conservative: small
# sessions retain conversational continuity, and unknown cache telemetry never
# triggers a destructive inference.
_CLAUDE_UNCACHED_SESSION_INPUT_LIMIT = 500_000
_CLAUDE_CACHE_USAGE_SCHEMA = 'separate_read_write_v1'
_CLAUDE_SESSION_ORIGIN_SCHEMA = 'stable_dynamic_sections_v1'
_CODING_MODEL_PROFILES = frozenset(('cheap', 'standard', 'strong'))


def _valid_claude_session_origin(value: Any, *, durable: bool) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    if (value.get('provider') != 'claude'
            or value.get('schema') != _CLAUDE_SESSION_ORIGIN_SCHEMA):
        return None
    origin = {'provider': 'claude', 'schema': _CLAUDE_SESSION_ORIGIN_SCHEMA}
    if durable:
        for key in ('origin_run_id', 'origin_session_id'):
            item = value.get(key)
            if not isinstance(item, str) or not item.strip() or len(item) > 512:
                return None
            origin[key] = item.strip()
    return origin


def _claude_session_origin(db, prior: Mapping[str, Any],
                           session_profile: Mapping[str, Any],
                           session_id: str) -> dict[str, str] | None:
    """Prove that the current session began with the stable prompt schema.

    A configuration emitted while resuming is never enough: it describes the
    current client, not the prompt that created the remote session. Fresh calls
    establish the marker. Later feedback runs inherit it from their durable
    feedback seed only while their provider event proves that lineage resumed.
    """
    inherited = None
    feedback_session = prior.get('feedback_session')
    if isinstance(feedback_session, Mapping):
        inherited = _valid_claude_session_origin(
            feedback_session.get('session_origin'), durable=True)
    rows = db.execute(
        "SELECT payload FROM events WHERE run_id=? AND type='provider.configuration' ORDER BY id",
        (prior['id'],),
    ).fetchall()
    origin = None
    observed_coding_configuration = False
    for row in rows:
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if (not isinstance(payload, dict)
                or payload.get('provider') != 'claude'
                or payload.get('model') != session_profile.get('model')
                or payload.get('read_only') is not False):
            continue
        first_coding_configuration = not observed_coding_configuration
        observed_coding_configuration = True
        strategy = payload.get('session_strategy')
        if strategy == 'fresh':
            marker = _valid_claude_session_origin(
                payload.get('session_origin'), durable=False)
            origin = ({**marker, 'origin_run_id': str(prior['id']),
                       'origin_session_id': session_id}
                      if marker is not None else None)
        elif strategy == 'resume':
            if first_coding_configuration:
                origin = inherited
        else:
            # An unversioned configuration cannot prove how the session began.
            origin = None
    return origin if observed_coding_configuration else None


def _feedback_session_strategy(db, prior: Mapping[str, Any], session_profile: Mapping[str, Any],
                               session_origin: Mapping[str, Any] | None) -> dict[str, Any]:
    """Choose resume versus a fresh model context from durable billed usage."""
    prior_id = str(prior['id'])
    default = {'strategy': 'resume', 'reason': 'verified_session_available'}
    if session_profile.get('provider') != 'claude':
        return default
    if _valid_claude_session_origin(session_origin, durable=True) is None:
        return {
            'strategy': 'cold_start',
            'reason': 'legacy_claude_session_origin_unknown',
            'previous_run_id': prior_id,
            'required_origin_schema': _CLAUDE_SESSION_ORIGIN_SCHEMA,
        }
    task_records = [*((prior.get('plan') or {}).get('tasks') or []),
                    *((prior.get('artifacts') or {}).get('tasks') or [])]
    coding_task_ids = {item.get('id') for item in task_records
                       if isinstance(item, Mapping)
                       and isinstance(item.get('id'), str) and item.get('id')}
    rows = db.execute(
        "SELECT id,task_id,payload FROM events WHERE run_id=? AND type='usage.recorded' ORDER BY id",
        (prior_id,),
    ).fetchall()
    grouped: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except (TypeError, json.JSONDecodeError):
            continue
        if (not isinstance(payload, dict)
                or payload.get('provider') != session_profile.get('provider')
                or payload.get('model') != session_profile.get('model')
                or payload.get('profile') not in _CODING_MODEL_PROFILES):
            continue
        if not coding_task_ids or row['task_id'] not in coding_task_ids:
            # Old events without a task identity, or an unexpected task using
            # the same model, cannot safely establish coding-session volume.
            return {**default, 'reason': 'cache_telemetry_untrusted'}
        call_id = payload.get('call_id')
        key = (('call', call_id) if isinstance(call_id, str) and call_id
               else ('event', row['id']))
        grouped.setdefault(key, []).append(payload)

    matched = []
    for (kind, _), payloads in grouped.items():
        trusted = None
        seen_trusted = False
        for payload in payloads:
            schema = payload.get('cache_usage_schema')
            incoming = payload.get('input_tokens')
            cached = payload.get('cached_input_tokens')
            valid = (schema == _CLAUDE_CACHE_USAGE_SCHEMA
                     and type(incoming) is int and incoming >= 0
                     and type(cached) is int and cached >= 0)
            if valid:
                usage = (incoming, cached)
                if trusted is not None and usage != trusted:
                    # Two invoices for one call disagree about the context
                    # volume. Never choose whichever happens to be last.
                    return {**default, 'reason': 'cache_telemetry_untrusted'}
                trusted = usage
                seen_trusted = True
                continue
            provisional = (schema is None
                and payload.get('input_tokens') is None
                and payload.get('cached_input_tokens') is None)
            if kind == 'event' or not provisional or seen_trusted:
                # Unknown -> known is a supported reconciliation. A later
                # unknown row, a legacy schema, or partial token data is not.
                return {**default, 'reason': 'cache_telemetry_untrusted'}
        if trusted is None:
            return {**default, 'reason': 'cache_telemetry_untrusted'}
        matched.append(trusted)
    if not matched:
        return default
    input_tokens = sum(item[0] for item in matched)
    cached_input_tokens = sum(item[1] for item in matched)
    if (input_tokens >= _CLAUDE_UNCACHED_SESSION_INPUT_LIMIT
            and cached_input_tokens == 0):
        return {
            'strategy': 'cold_start',
            'reason': 'large_uncached_claude_session',
            'previous_run_id': prior_id,
            'input_tokens': input_tokens,
            'cached_input_tokens': 0,
            'input_limit': _CLAUDE_UNCACHED_SESSION_INPUT_LIMIT,
        }
    return {**default, 'input_tokens': input_tokens,
            'cached_input_tokens': cached_input_tokens,
            'input_limit': _CLAUDE_UNCACHED_SESSION_INPUT_LIMIT}


def _json(value: Any) -> str:
    return json.dumps(scrub(value), ensure_ascii=False, separators=(",", ":"))


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} 必须是长度不超过 {limit} 的文本")
    return value


def _validate_model_entry(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} 必须是对象")
    unknown = set(value) - {"provider", "model", "parameters"}
    if unknown:
        raise ValueError(f"{where} 包含未知参数")
    provider = value.get("provider", "")
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"{where}.provider 不受支持")
    model = value.get("model", "")
    if not isinstance(model, str) or len(model) > 300:
        raise ValueError(f"{where}.model 必须是文本")
    parameters = value.get("parameters", {})
    if not isinstance(parameters, Mapping) or set(parameters) - MODEL_PARAMETERS:
        raise ValueError(f"{where}.parameters 包含未知参数")
    if parameters:
        raise ValueError(f"{where} 模型参数暂不支持，不能静默丢弃")
    if "temperature" in parameters and (type(parameters["temperature"]) not in (int, float) or not 0 <= parameters["temperature"] <= 2):
        raise ValueError(f"{where}.temperature 无效")
    if "top_p" in parameters and (type(parameters["top_p"]) not in (int, float) or not 0 <= parameters["top_p"] <= 1):
        raise ValueError(f"{where}.top_p 无效")
    if "max_tokens" in parameters and (type(parameters["max_tokens"]) is not int or not 1 <= parameters["max_tokens"] <= 1_000_000):
        raise ValueError(f"{where}.max_tokens 无效")
    if "reasoning_effort" in parameters and parameters["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError(f"{where}.reasoning_effort 无效")
    return {"provider": provider, "model": model, "parameters": dict(parameters)}


def _validate_model_settings(value: Any) -> dict[str, Any]:
    if value is None:
        return {"default": {"provider": "codex", "model": "", "parameters": {}}}
    if not isinstance(value, Mapping):
        raise ValueError("model_settings 必须是对象")
    unknown = set(value) - MODEL_STAGES
    if unknown:
        raise ValueError("model_settings 包含未知阶段")
    return {str(stage): _validate_model_entry(item, f"model_settings.{stage}") for stage, item in value.items()}


def _validate_list(value: Any, name: str, limit: int, item_limit: int = 4000) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError(f"{name} 必须是最多 {limit} 项的列表")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > item_limit:
            raise ValueError(f"{name} 包含无效文本")
        result.append(item)
    return result


def _validate_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("职能体配置必须是对象")
    unknown = set(value) - PATCH_FIELDS
    if unknown:
        raise ValueError("职能体配置包含未知字段")
    delivery = value.get("delivery", {})
    if not isinstance(delivery, Mapping) or set(delivery) - {"description", "format", "artifacts"}:
        raise ValueError("delivery 包含未知字段")
    delivery = dict(delivery)
    if "description" in delivery: delivery["description"] = _text(delivery["description"], "delivery.description", 10000)
    if "format" in delivery: delivery["format"] = _text(delivery["format"], "delivery.format", 200)
    if "artifacts" in delivery: delivery["artifacts"] = _validate_list(delivery["artifacts"], "delivery.artifacts", 100, 300)
    skills = _validate_list(value.get("skill_ids", []), "skill_ids", 500, 100)
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", x) for x in skills): raise ValueError("skill_ids 包含无效 ID")
    tools = _validate_list(value.get("tool_scope", []), "tool_scope", 200, 300)
    if tools:
        raise ValueError("本版只支持继承平台工具范围，tool_scope 必须为空")
    return {"instructions": _text(value.get("instructions", ""), "instructions", 100000),
            "model_settings": _validate_model_settings(value.get("model_settings")),
            "tool_scope": tools,
            "acceptance": _validate_list(value.get("acceptance", []), "acceptance", 100),
            "delivery": delivery, "skill_ids": skills}


def _safe_name(raw: str) -> tuple[str, str]:
    name = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    parts = name.split("/")
    if not name or name.startswith("/") or ":" in name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or any(not p or p in {".", ".."} for p in parts): raise ValueError("Skill 包含非法或越界路径")
    if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", parts[-1].casefold()): raise ValueError("Skill 包含 Windows 保留文件名")
    return name, "/".join(p.casefold() for p in parts)


def _check_skill_yaml(path: str, body: bytes, files: set[str]) -> None:
    if not path.lower().endswith((".yaml", ".yml")): return
    try:
        import yaml
        value = yaml.safe_load(body.decode("utf-8"))
    except Exception as exc: raise ValueError(f"Skill YAML 不安全或无法解析: {path}") from exc
    if isinstance(value, Mapping):
        for key in ("entry", "entrypoint", "main", "script"):
            ref = value.get(key)
            if ref is not None and (not isinstance(ref, str) or ref.replace("\\", "/").casefold() not in files): raise ValueError(f"Skill 入口引用不存在: {path}")


def _check_skill_markdown(path: str, body: bytes, files: set[str]) -> None:
    if path.casefold().split("/")[-1] != "skill.md": return
    try: text = body.decode("utf-8")
    except UnicodeDecodeError as exc: raise ValueError("Skill.md 必须是 UTF-8") from exc
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end < 0: raise ValueError("Skill.md frontmatter 不完整")
        _check_skill_yaml("SKILL.md", text[3:end].encode("utf-8"), files)
    for match in re.finditer(r"(?im)^\s*(?:entry(?:point)?|main|script)\s*:\s*[`\"]?([^`\"\s]+)", text):
        if match.group(1).replace("\\", "/").casefold() not in files: raise ValueError("Skill 入口引用不存在: SKILL.md")


class AgentStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_versions(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(agent_id,version));
            CREATE TABLE IF NOT EXISTS agent_drafts(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skill_assets(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, data TEXT NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_conversations(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS agent_versions_agent ON agent_versions(agent_id,version);
            CREATE INDEX IF NOT EXISTS agent_conversations_agent ON agent_conversations(agent_id);
            CREATE TRIGGER IF NOT EXISTS no_agent_version_update BEFORE UPDATE ON agent_versions BEGIN SELECT RAISE(ABORT,'agent versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS no_agent_version_delete BEFORE DELETE ON agent_versions BEGIN SELECT RAISE(ABORT,'agent versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS no_skill_update BEFORE UPDATE ON skill_assets BEGIN SELECT RAISE(ABORT,'skill assets are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS no_skill_delete BEFORE DELETE ON skill_assets BEGIN SELECT RAISE(ABORT,'skill assets are immutable'); END;
            """)

    @staticmethod
    def _decode(row): return json.loads(row[0])
    def _row(self, table, key, value):
        with self.store.connect() as db: row = db.execute(f"SELECT data FROM {table} WHERE {key}=?", (value,)).fetchone()
        if not row: raise KeyError(value)
        return self._decode(row)
    def list(self):
        with self.store.connect() as db: return [self._decode(r) for r in db.execute("SELECT data FROM agents ORDER BY rowid DESC")]
    def get(self, aid): return self._row("agents", "id", aid)

    def create(self, data, actor):
        if not isinstance(data, Mapping) or not isinstance(data.get("name"), str) or not data["name"].strip(): raise ValueError("职能体名称不能为空")
        aid, at = uuid.uuid4().hex, now(); payload = _validate_payload({k: data[k] for k in PATCH_FIELDS if k in data})
        agent = {"id": aid, "name": data["name"].strip()[:120], "purpose": _text(data.get("purpose", ""), "purpose", 4000), "active_version": 1, "created_at": at, "updated_at": at, "actor": actor}
        version = {"id": uuid.uuid4().hex, "agent_id": aid, "version": 1, **payload, "source": "created", "previous_version": None, "created_at": at}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); db.execute("INSERT INTO agents VALUES (?,?)", (aid, _json(agent))); db.execute("INSERT INTO agent_versions VALUES (?,?,?,?,?)", (version["id"], aid, 1, _json(version), at))
        return {**agent, "version": version}

    def versions(self, aid):
        self.get(aid)
        with self.store.connect() as db: return [self._decode(r) for r in db.execute("SELECT data FROM agent_versions WHERE agent_id=? ORDER BY version DESC", (aid,))]
    def version(self, aid, number=None):
        self.get(aid)
        with self.store.connect() as db:
            row = db.execute("SELECT data FROM agent_versions WHERE agent_id=? AND version=COALESCE(?,json_extract((SELECT data FROM agents WHERE id=?),'$.active_version'))", (aid, number, aid)).fetchone()
        if not row: raise KeyError(aid if number is None else number)
        return self._decode(row)
    def _draft_from_version(self, aid, v):
        return {"id": uuid.uuid4().hex, "agent_id": aid, "base_version": v["version"], "revision": 0, "patch": {k: v[k] for k in PATCH_FIELDS}, "conflicts": [], "explanation": [], "updated_at": now()}
    def draft(self, aid):
        self.get(aid)
        with self.store.connect() as db:
            row = db.execute("SELECT data FROM agent_drafts WHERE agent_id=?", (aid,)).fetchone()
            if row: return self._decode(row)
            vrow = db.execute("SELECT data FROM agent_versions WHERE agent_id=? AND version=json_extract((SELECT data FROM agents WHERE id=?),'$.active_version')", (aid, aid)).fetchone()
        return self._draft_from_version(aid, self._decode(vrow))
    def _validate_patch_against(self, patch, current, *, allow_scope_change=False, allow_acceptance_relax=False):
        if not isinstance(patch, Mapping) or set(patch) - PATCH_FIELDS: raise ValueError("草稿包含不可修改字段")
        candidate = _validate_payload({k: patch[k] if k in patch else current[k] for k in PATCH_FIELDS})
        if not allow_scope_change and candidate["tool_scope"] != current["tool_scope"]: raise ValueError("维护资料不能改变工具范围")
        if not allow_acceptance_relax and not set(current["acceptance"]).issubset(candidate["acceptance"]): raise ValueError("维护资料不能放松既有验收条件")
        return candidate
    def save_draft(self, aid, patch, expected_revision, *, conflicts=None, explanation=None, allow_scope_change=False, allow_acceptance_relax=False, _db=None):
        if type(expected_revision) is not int or expected_revision < 0: raise ValueError("expected_revision 必须是非负整数")
        with (nullcontext(_db) if _db is not None else self.store.connect()) as db:
            if _db is None: db.execute("BEGIN IMMEDIATE")
            arow = db.execute("SELECT 1 FROM agents WHERE id=?", (aid,)).fetchone()
            if not arow: raise KeyError(aid)
            row = db.execute("SELECT data FROM agent_drafts WHERE agent_id=?", (aid,)).fetchone()
            if row: current = self._decode(row)
            else:
                vrow = db.execute("SELECT data FROM agent_versions WHERE agent_id=? AND version=json_extract((SELECT data FROM agents WHERE id=?),'$.active_version')", (aid, aid)).fetchone(); current = self._draft_from_version(aid, self._decode(vrow))
            if current["revision"] != expected_revision: raise Conflict("职能体草稿已更新，请重新加载")
            merged_patch = self._validate_patch_against(patch, current["patch"], allow_scope_change=allow_scope_change, allow_acceptance_relax=allow_acceptance_relax)
            missing = [sid for sid in merged_patch["skill_ids"] if not db.execute("SELECT 1 FROM skill_assets WHERE id=? AND agent_id=?", (sid, aid)).fetchone()]
            if missing: raise ValueError("草稿引用了不属于该职能体的 Skill")
            merged = {**current, "patch": merged_patch, "revision": expected_revision + 1, "updated_at": now(),
                      "conflicts": list(conflicts or []), "explanation": list(explanation or []),
                      "requires_scope_approval": bool(allow_scope_change and merged_patch["tool_scope"] != current["patch"]["tool_scope"]),
                      "requires_acceptance_approval": bool(allow_acceptance_relax and not set(current["patch"]["acceptance"]).issubset(merged_patch["acceptance"]))}
            db.execute("INSERT OR REPLACE INTO agent_drafts VALUES (?,?,?,?)", (merged["id"], aid, _json(merged), merged["updated_at"]))
        return merged
    def set_draft_metadata(self, aid, *, conflicts=None, explanation=None, expected_revision=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision 必须用于并发更新")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT data FROM agent_drafts WHERE agent_id=?", (aid,)).fetchone()
            if not row: raise KeyError(aid)
            d = self._decode(row)
            if expected_revision is not None and d["revision"] != expected_revision: raise Conflict("职能体草稿已更新，请重新加载")
            d.update(conflicts=list(conflicts or []), explanation=list(explanation or []), revision=d["revision"] + 1, updated_at=now()); db.execute("UPDATE agent_drafts SET data=?,updated_at=? WHERE agent_id=?", (_json(d), d["updated_at"], aid))
        return d
    def apply(self, aid, expected_revision, idem=None, *, allow_scope_change=False, allow_acceptance_relax=False):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); arow = db.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
            if not arow: raise KeyError(aid)
            agent = self._decode(arow); versions = [self._decode(r) for r in db.execute("SELECT data FROM agent_versions WHERE agent_id=? ORDER BY version DESC", (aid,))]
            row = db.execute("SELECT data FROM agent_drafts WHERE agent_id=?", (aid,)).fetchone()
            if row: d = self._decode(row)
            else: d = self._draft_from_version(aid, next(v for v in versions if v["version"] == agent["active_version"]))
            requested_fingerprint = hashlib.sha256(json.dumps(d["patch"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if idem:
                matches = [v for v in versions if v.get("idempotency_key") == idem]
                if matches:
                    if matches[0].get("idempotency_fingerprint") != requested_fingerprint: raise Conflict("相同幂等键对应了不同应用内容")
                    return matches[0]
            if d["revision"] != expected_revision: raise Conflict("职能体草稿已更新，请重新加载")
            if d.get("conflicts"): raise Conflict("草稿存在未决冲突，请先解决后再应用")
            if d.get("base_version") != agent["active_version"]: raise Conflict("草稿基于旧版本，请重新整理")
            if d.get("requires_scope_approval") and not allow_scope_change: raise Conflict("工具范围变化需要明确授权")
            if d.get("requires_acceptance_approval") and not allow_acceptance_relax: raise Conflict("放宽验收条件需要明确授权")
            ver = max(v["version"] for v in versions) + 1; at = now(); payload = _validate_payload(d["patch"])
            data = {"id": uuid.uuid4().hex, "agent_id": aid, "version": ver, **payload, "previous_version": next(v["id"] for v in versions if v["version"] == agent["active_version"]), "source": "draft", "idempotency_key": idem, "idempotency_fingerprint": requested_fingerprint, "created_at": at}
            db.execute("INSERT INTO agent_versions VALUES (?,?,?,?,?)", (data["id"], aid, ver, _json(data), at)); agent.update(active_version=ver, updated_at=at); db.execute("UPDATE agents SET data=? WHERE id=?", (_json(agent), aid))
            if row: db.execute("DELETE FROM agent_drafts WHERE agent_id=?", (aid,))
        return data
    def rollback(self, aid, version, *, expected_active_version=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); arow = db.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
            if not arow: raise KeyError(aid)
            target_row = db.execute("SELECT data FROM agent_versions WHERE agent_id=? AND version=?", (aid, version)).fetchone()
            if not target_row: raise KeyError(version)
            agent = self._decode(arow)
            if expected_active_version is not None and agent.get("active_version") != expected_active_version: raise Conflict("职能体版本已更新，请重新加载")
            target = self._decode(target_row); ver = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM agent_versions WHERE agent_id=?", (aid,)).fetchone()[0]; at = now()
            data = {k: target[k] for k in PATCH_FIELDS}; data.update(id=uuid.uuid4().hex, agent_id=aid, version=ver, previous_version=target["id"], source="rollback", rollback_of=target["version"], created_at=at)
            db.execute("INSERT INTO agent_versions VALUES (?,?,?,?,?)", (data["id"], aid, ver, _json(data), at)); agent.update(active_version=ver, updated_at=at); db.execute("UPDATE agents SET data=? WHERE id=?", (_json(agent), aid))
            db.execute("DELETE FROM agent_drafts WHERE agent_id=?", (aid,))
        return {**agent, "version": data}
    def conversation(self, cid):
        c = self._row("agent_conversations", "id", cid)
        c['pending_feedback_count'] = sum(m.get('feedback_status') == 'pending' for m in c['messages'])
        return c

    def adopt_feedback(self, cid):
        """Atomically consume a feedback batch and link its successor run.

        The transaction is the durable outbox: a crash can leave an unqueued
        received run, but never consumed feedback without its run or two runs.
        """
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            c = self._decode(db.execute('SELECT data FROM agent_conversations WHERE id=?', (cid,)).fetchone())
            pending = [m for m in c['messages'] if m.get('feedback_status') == 'pending']
            if not pending or not c.get('run_id'):
                return None
            prior = self._decode(db.execute('SELECT data FROM runs WHERE id=?', (c['run_id'],)).fetchone())
            applied = set(prior.get('feedback_applied_ids', []))
            for m in pending:
                if m['id'] in applied:
                    m.update(feedback_status='adopted', feedback_run_id=prior['id'])
            if applied:
                db.execute('UPDATE agent_conversations SET data=? WHERE id=?', (_json(c), cid))
            pending = [m for m in pending if m['id'] not in applied]
            if not pending or prior['status'] not in ('ready_for_review', 'published'):
                return None
            # Keep each current batch out of the deliberately bounded historical
            # context. At most 50k characters per successor; larger queues are
            # retained for subsequent safe boundaries without truncating input.
            batch, size = [], 0
            for m in pending:
                if batch and size + len(m['content']) > 50_000:
                    break
                batch.append(m)
                size += len(m['content'])
            pending = batch
            rid, at = uuid.uuid4().hex, now()
            source = {**prior.get('source', {}), 'type': 'agent', 'conversation_id': cid,
                      'feedback_of': prior['id']}
            data = dict(id=rid, project_id=prior['project_id'],
                request='在上一轮已验证成果基础上完成以下补充需求：\n' + '\n\n'.join(m['content'] for m in pending), history=[*prior.get('history', []), prior['request'],
                    '用户补充（在上一轮成果基础上继续）：\n' + '\n\n'.join(m['content'] for m in pending)],
                status='received', revision=0, plan=None, triage=None, tasks=[], artifacts={},
                root_request=prior.get('root_request', prior['request']),
                authorization_requests=list(dict.fromkeys([*prior.get('authorization_requests', []), prior['request']])),
                source=source, created_at=at, updated_at=at, feedback_predecessor_id=prior['id'])
            for key in ('agent_id', 'agent_version', 'agent_snapshot', 'conversation_id', 'runtime_configuration'):
                if key in prior:
                    data[key] = prior[key]
            # A feedback successor gets a fresh run, branch, worktree and
            # artifact ledger.  The only execution state that may cross this
            # verified boundary is the provider session identity.  The
            # executor validates provider compatibility again before using it.
            # A model change within the same provider remains resumable and is
            # recorded by the execution event; a provider change falls back to
            # full context.
            artifacts = prior.get('artifacts') or {}
            session_id = artifacts.get('session_id')
            session_profile = artifacts.get('session_profile') or {}
            session_strategy = {'strategy': 'unavailable', 'reason': 'verified_session_unavailable'}
            if (artifacts.get('execution_mode') == 'continuous'
                    and all(artifacts.get(key) for key in ('worktree', 'branch', 'commit'))
                    and isinstance(session_id, str) and 0 < len(session_id.strip()) <= 512
                    and isinstance(session_profile, dict)
                    and all(isinstance(session_profile.get(key), str)
                            and session_profile[key].strip()
                            for key in ('provider', 'model'))):
                reusable_profile = {
                    'provider': session_profile['provider'].strip(),
                    'model': session_profile['model'].strip(),
                }
                session_origin = (_claude_session_origin(
                    db, prior, reusable_profile, session_id.strip())
                    if reusable_profile['provider'] == 'claude' else None)
                session_strategy = _feedback_session_strategy(
                    db, prior, reusable_profile, session_origin)
                if session_strategy['strategy'] == 'resume':
                    data['feedback_session'] = {
                        'session_id': session_id.strip(),
                        'session_profile': reusable_profile,
                        'previous_run_id': prior['id'],
                    }
                    if session_origin is not None:
                        data['feedback_session']['session_origin'] = session_origin
            data['feedback_session_strategy'] = session_strategy
            db.execute('INSERT INTO runs VALUES (?,?)', (rid, _json(data)))
            self.store._event(db, rid, 'feedback.adopted', {'previous_run_id': prior['id'],
                'message_ids': [m['id'] for m in pending],
                'session_continuation_available': bool(data.get('feedback_session')),
                'session_strategy': session_strategy})
            for m in pending:
                m.update(feedback_status='adopted', feedback_run_id=rid)
            c.update(run_id=rid, updated_at=at)
            c['messages'].append({'id': uuid.uuid4().hex, 'role': 'assistant',
                'content': '已自动接续补充需求，正在基于上一轮成果继续工作。',
                'created_at': at, 'status': 'completed', 'run_id': rid})
            db.execute('UPDATE agent_conversations SET data=? WHERE id=?', (_json(c), cid))
            return data

    def create_retry(self, prior, actor, actor_id, history):
        """Create the complete retry and move its conversation in one commit."""
        key = f"retry:{prior['id']}:{prior['revision']}"
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT run_id FROM deliveries WHERE id=?', (key,)).fetchone()
            if existing:
                return self._decode(db.execute('SELECT data FROM runs WHERE id=?', (existing[0],)).fetchone()), False
            rid, at = uuid.uuid4().hex, now()
            data = dict(id=rid, project_id=prior['project_id'], request=prior['request'],
                status='received', revision=0, plan=None, triage=None, tasks=[], artifacts={},
                history=history, created_at=at, updated_at=at, previous_run_id=prior['id'],
                source={'type': 'retry', 'actor': actor, 'actor_id': actor_id if actor_id is not None else prior.get('source', {}).get('actor_id'), 'retry_of': prior['id']})
            for field in ('capability', 'agent_id', 'agent_version', 'agent_snapshot',
                          'runtime_configuration', 'conversation_id', 'feedback_predecessor_id', 'feedback_applied_ids',
                          'root_request', 'authorization_requests', 'module_snapshot', 'module_selection_revision', 'mount_snapshot'):
                if field in prior:
                    data[field] = prior[field]
            cid = prior.get('conversation_id')
            if cid:
                c = self._decode(db.execute('SELECT data FROM agent_conversations WHERE id=?', (cid,)).fetchone())
                if c.get('run_id') != prior['id']:
                    raise Conflict('会话已有后续任务，请在当前任务上继续')
                c.update(run_id=rid, updated_at=at)
                db.execute('UPDATE agent_conversations SET data=? WHERE id=?', (_json(c), cid))
            db.execute('INSERT INTO runs VALUES (?,?)', (rid, _json(data)))
            db.execute('INSERT INTO deliveries VALUES (?,?)', (key, rid))
            self.store._event(db, rid, 'run.retry_created', {'previous_run_id': prior['id'], 'actor': actor})
            self.store._event(db, prior['id'], 'run.retry_linked', {'next_run_id': rid, 'actor': actor})
            return data, True

    def feedback_error(self, cid, message):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            c = self._decode(db.execute('SELECT data FROM agent_conversations WHERE id=?', (cid,)).fetchone())
            if c.get('feedback_error') != message:
                c['feedback_error'] = message
                db.execute('UPDATE agent_conversations SET data=? WHERE id=?', (_json(c), cid))

    def settle_feedback(self, cid, rid, message_ids=None):
        """A direct clarification already includes these pending messages."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            c = self._decode(db.execute('SELECT data FROM agent_conversations WHERE id=?', (cid,)).fetchone())
            for m in c['messages']:
                if m.get('feedback_status') == 'pending' and (message_ids is None or m['id'] in message_ids):
                    m.update(feedback_status='adopted', feedback_run_id=rid)
            db.execute('UPDATE agent_conversations SET data=? WHERE id=?', (_json(c), cid))

    def conversations(self, aid):
        self.get(aid)
        with self.store.connect() as db: return [self._decode(r) for r in db.execute("SELECT data FROM agent_conversations WHERE agent_id=? ORDER BY rowid DESC", (aid,))]
    def create_conversation(self, aid, mode, project_id=None, actor_id=None):
        if mode not in {"do", "maintain"}: raise ValueError("会话模式无效")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); arow = db.execute("SELECT 1 FROM agents WHERE id=?", (aid,)).fetchone()
            if not arow: raise KeyError(aid)
            vrow = db.execute("SELECT data FROM agent_versions WHERE agent_id=? AND version=json_extract((SELECT data FROM agents WHERE id=?),'$.active_version')", (aid, aid)).fetchone(); drow = db.execute("SELECT data FROM agent_drafts WHERE agent_id=?", (aid,)).fetchone(); cid = uuid.uuid4().hex; at = now()
            draft_revision = json.loads(drow[0]).get("revision", 0) if drow else 0
            c = {"id": cid, "agent_id": aid, "mode": mode, "project_id": project_id, "actor_id": actor_id, "messages": [], "run_id": None, "draft_revision": draft_revision, "created_at": at, "updated_at": at}
            db.execute("INSERT INTO agent_conversations VALUES (?,?,?)", (cid, aid, _json(c)))
        return c
    def append_message(self, cid, role, content, expected_updated_at=None, **extra):
        if role not in {"user", "assistant", "system", "tool"}: raise ValueError("消息角色无效")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT data FROM agent_conversations WHERE id=?", (cid,)).fetchone()
            if not row: raise KeyError(cid)
            c = self._decode(row)
            if expected_updated_at is not None and c.get("updated_at") != expected_updated_at: raise Conflict("会话已更新，请重新加载")
            c["messages"].append({"id": uuid.uuid4().hex, "role": role, "content": scrub(content), "created_at": now(), **scrub(extra)}); c["updated_at"] = now(); db.execute("UPDATE agent_conversations SET data=? WHERE id=?", (_json(c), cid))
        return c
    def attach_run(self, cid, rid, expected_updated_at=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT data FROM agent_conversations WHERE id=?", (cid,)).fetchone()
            if not row: raise KeyError(cid)
            c = self._decode(row)
            if expected_updated_at is not None and c.get("updated_at") != expected_updated_at: raise Conflict("会话已更新，请重新加载")
            c.update(run_id=rid, updated_at=now()); db.execute("UPDATE agent_conversations SET data=? WHERE id=?", (_json(c), cid))
        return c
    def skill_body(self, sid, path=None, agent_id=None):
        with self.store.connect() as db:
            row = db.execute("SELECT agent_id,content FROM skill_assets WHERE id=?", (sid,)).fetchone()
        if not row: raise KeyError(sid)
        if agent_id is not None and row[0] != agent_id: raise PermissionError("Skill 不属于该职能体")
        raw = bytes(row[1])
        if path is None: return raw
        wanted = unicodedata.normalize("NFC", path.replace("\\", "/"))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for info in z.infolist():
                if unicodedata.normalize("NFC", info.filename.replace("\\", "/")) == wanted: return z.read(info)
        raise KeyError(path)


def inspect_skill(raw: bytes):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_ZIP: raise ValueError("Skill 压缩包超过 20 MiB")
    digest = hashlib.sha256(raw).hexdigest(); total = 0; names = []; folded = set(); files = set()
    try: z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile: raise ValueError("Skill 必须是有效 ZIP") from None
    infos = z.infolist()
    if len(infos) > MAX_FILES: raise ValueError("Skill 文件数超过 500")
    for info in infos:
        name, key = _safe_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
        if key in folded: raise ValueError("Skill 包含 Unicode/大小写重复路径")
        folded.add(key); mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000: raise ValueError("Skill 不允许符号链接")
        if info.is_dir(): continue
        if info.file_size > MAX_FILE: raise ValueError("Skill 单文件超过 2 MiB")
        if info.file_size and info.compress_size and info.file_size / max(1, info.compress_size) > 100: raise ValueError("Skill 压缩比超过 100:1")
        total += info.file_size
        if total > MAX_UNPACKED: raise ValueError("Skill 展开后超过 100 MiB")
        body = z.read(info); files.add(name); names.append({"path": name, "size": info.file_size, "sha256": hashlib.sha256(body).hexdigest()})
    canonical_files = {p.casefold() for p in files}
    for info in infos:
        if not info.is_dir():
            path = info.filename.replace("\\", "/"); body = z.read(info)
            _check_skill_yaml(path, body, canonical_files); _check_skill_markdown(path, body, canonical_files)
    return {"sha256": digest, "files": names, "size_bytes": len(raw), "unpacked_bytes": total}


__all__ = ["AgentStore", "inspect_skill", "MAX_ZIP", "MAX_UNPACKED", "MAX_FILES", "MAX_FILE"]
