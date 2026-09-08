"""Versioned Agent capability contracts.

Capabilities are durable user configuration.  A current pointer is updated with
CAS while every body is kept in an append-only version table, so editing a
capability never changes a version already bound to a project or frozen on a run.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping

from factory.control.store import Conflict, now, scrub


CATEGORIES = {"engineering", "operations", "collaboration", "integration", "migration", "custom"}
STATUSES = {"draft", "ready"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _string(value, field: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if "\x00" in value:
        raise ValueError(f"{field} contains an invalid character")
    value = scrub(value)
    if required and not value.strip():
        raise ValueError(f"{field} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    return value.strip() if field in {"name", "category"} else value


def _id(value, field: str) -> str:
    value = _string(value, field, 200)
    return value


def _revision(value, field: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _acceptance(value, *, required: bool) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("acceptance must be a list")
    if len(value) > 20:
        raise ValueError("acceptance must contain at most 20 requirements")
    result = []
    for item in value:
        result.append(_string(item, "acceptance item", 2000))
    if required and not result:
        raise ValueError("acceptance must not be empty for a ready capability")
    return result


def _capability_data(data: Mapping, *, allow_status: bool = True) -> dict:
    if not isinstance(data, Mapping):
        raise ValueError("capability body must be an object")
    allowed = {
        "name", "description", "category", "instructions", "input_description",
        "output_description", "acceptance", "status",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("unknown capability fields")
    status = data.get("status", "draft")
    if not allow_status:
        status = "draft"
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("status must be draft or ready")
    result = {
        "name": _string(data.get("name"), "name", 120),
        "description": _string(data.get("description", ""), "description", 4000, required=False),
        "category": _string(data.get("category"), "category", 40),
        "instructions": _string(data.get("instructions"), "instructions", 12000),
        "input_description": _string(data.get("input_description"), "input_description", 4000),
        "output_description": _string(data.get("output_description"), "output_description", 4000),
        "acceptance": _acceptance(data.get("acceptance", []), required=status == "ready"),
        "status": status,
    }
    if result["category"] not in CATEGORIES:
        raise ValueError("invalid capability category")
    return result


TEMPLATES = (
    {
        "id": "template-project-maintenance",
        "name": "项目维护",
        "description": "整理依赖、修复小型缺陷并执行项目既有检查。",
        "category": "engineering",
        "instructions": "在项目配置的工作区内处理维护请求，遵守既有修改范围、检查和发布策略。此模板仍是草稿，配置完整验收条件后才能调用。",
        "input_description": "维护目标、影响范围和需要保留的行为。",
        "output_description": "修改摘要、检查结果、风险和交付证据。",
        "acceptance": [],
        "status": "draft",
    },
    {
        "id": "template-cloud-deployment",
        "name": "云部署",
        "description": "按已配置环境和发布检查准备云端部署。",
        "category": "operations",
        "instructions": "根据项目已授权的部署流程准备变更并验证结果。不得假设凭据、云资源或部署成功；模板需由用户补齐具体契约。",
        "input_description": "部署目标、环境、回滚条件和可用检查。",
        "output_description": "部署变更、验证证据、回滚信息和未完成事项。",
        "acceptance": [],
        "status": "draft",
    },
    {
        "id": "template-feishu-progress-reporting",
        "name": "飞书进度汇报",
        "description": "从运行记录整理项目进度和阻塞事项。",
        "category": "collaboration",
        "instructions": "只依据运行事件和项目事实整理进度，明确未知信息与需要人工处理的事项。模板未配置接收范围和发送权限前不得调用。",
        "input_description": "运行范围、报告周期、收件人和允许的消息渠道。",
        "output_description": "结构化进度摘要、证据链接、风险和待办。",
        "acceptance": [],
        "status": "draft",
    },
    {
        "id": "template-external-api-integration",
        "name": "外部 API 集成",
        "description": "在明确授权范围内设计和验证外部服务集成。",
        "category": "integration",
        "instructions": "根据已配置 API 契约、权限和测试端点实现集成；不得凭空生成凭据、外部状态或调用成功结论。",
        "input_description": "服务契约、认证方式、数据范围、超时和验收样例。",
        "output_description": "集成变更、请求与响应证据、错误处理和安全边界。",
        "acceptance": [],
        "status": "draft",
    },
    {
        "id": "template-legacy-migration",
        "name": "遗留系统迁移",
        "description": "规划受控的遗留系统迁移并保留回退证据。",
        "category": "migration",
        "instructions": "先记录迁移范围、依赖、数据核验和回退方案，再按阶段执行。所有外部变更须由项目权限和验收条件明确授权。",
        "input_description": "源系统、目标系统、数据范围、停机窗口和回退条件。",
        "output_description": "迁移计划、核验结果、风险、回退点和交付证据。",
        "acceptance": [],
        "status": "draft",
    },
)


class CapabilityStore:
    """SQLite-backed immutable capability revisions and project bindings."""

    def __init__(self, store):
        self.store = store
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS capabilities(
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_versions(
                    id TEXT NOT NULL, revision INTEGER NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL,
                    category TEXT NOT NULL, instructions TEXT NOT NULL,
                    input_description TEXT NOT NULL, output_description TEXT NOT NULL,
                    acceptance TEXT NOT NULL, status TEXT NOT NULL,
                    source_run_id TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(id, revision)
                );
                CREATE TABLE IF NOT EXISTS capability_bindings(
                    project_id TEXT NOT NULL, capability_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, actor TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, capability_id)
                );
                CREATE TABLE IF NOT EXISTS capability_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT,
                    project_id TEXT, type TEXT NOT NULL, actor TEXT NOT NULL,
                    payload TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS no_capability_version_update
                    BEFORE UPDATE ON capability_versions BEGIN
                    SELECT RAISE(ABORT,'capability versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_capability_version_delete
                    BEFORE DELETE ON capability_versions BEGIN
                    SELECT RAISE(ABORT,'capability versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_capability_audit_update
                    BEFORE UPDATE ON capability_audit BEGIN
                    SELECT RAISE(ABORT,'capability audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_capability_audit_delete
                    BEFORE DELETE ON capability_audit BEGIN
                    SELECT RAISE(ABORT,'capability audit is append-only'); END;
                """
            )
            self._seed(db)

    @staticmethod
    def _audit(db, *, capability_id=None, project_id=None, kind, actor, payload):
        db.execute(
            "INSERT INTO capability_audit(capability_id,project_id,type,actor,payload,at) VALUES (?,?,?,?,?,?)",
            (capability_id, project_id, kind, actor, _json(scrub(payload)), now()),
        )

    @staticmethod
    def _view(row, *, version_created=False):
        if row is None:
            return None
        return {
            "id": row["id"], "revision": int(row["revision"]),
            "name": row["name"], "description": row["description"],
            "category": row["category"], "instructions": row["instructions"],
            "input_description": row["input_description"],
            "output_description": row["output_description"],
            "acceptance": json.loads(row["acceptance"]), "status": row["status"],
            "created_at": row["version_created_at"] if version_created else row["capability_created_at"],
            "updated_at": row["version_created_at"] if version_created else row["updated_at"],
            **({"source_run_id": row["source_run_id"]} if row["source_run_id"] else {}),
        }

    @staticmethod
    def _version_select():
        return (
            "SELECT v.*, v.created_at AS version_created_at, "
            "c.created_at AS capability_created_at, c.updated_at AS updated_at "
            "FROM capability_versions v JOIN capabilities c ON c.id=v.id "
        )

    def _seed(self, db):
        # Multiple resumed runs can initialize their capability context at
        # once. Serialize the existence check and insertion as one transaction.
        db.execute('BEGIN IMMEDIATE')
        for template in TEMPLATES:
            if db.execute("SELECT 1 FROM capabilities WHERE id=?", (template["id"],)).fetchone():
                continue
            at = now()
            body = _capability_data({key: value for key, value in template.items() if key != "id"})
            db.execute("INSERT INTO capabilities VALUES (?,?,?,?)", (template["id"], 1, at, at))
            db.execute(
                "INSERT INTO capability_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (template["id"], 1, body["name"], body["description"], body["category"],
                 body["instructions"], body["input_description"], body["output_description"],
                 _json(body["acceptance"]), body["status"], None, at),
            )
            self._audit(db, capability_id=template["id"], kind="capability.seeded",
                        actor="system", payload={"revision": 1, "template": True})

    def get(self, capability_id, revision=None):
        capability_id = _id(capability_id, "capability_id")
        if revision is not None:
            revision = _revision(revision)
        with self.store.connect() as db:
            if revision is None:
                row = db.execute(self._version_select() + "WHERE v.id=? AND v.revision=c.revision", (capability_id,)).fetchone()
            else:
                row = db.execute(self._version_select() + "WHERE v.id=? AND v.revision=?", (capability_id, revision)).fetchone()
            if row is None:
                raise KeyError(capability_id)
            return self._view(row)

    def versions(self, capability_id):
        capability_id = _id(capability_id, "capability_id")
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM capabilities WHERE id=?", (capability_id,)).fetchone()
            if exists is None:
                raise KeyError(capability_id)
            rows = db.execute(self._version_select() + "WHERE v.id=? ORDER BY v.revision", (capability_id,)).fetchall()
            return [self._view(row, version_created=True) for row in rows]

    def list(self):
        with self.store.connect() as db:
            rows = db.execute(self._version_select() + "WHERE v.revision=c.revision ORDER BY v.created_at, v.id").fetchall()
            return [self._view(row) for row in rows]

    def create(self, data, *, actor="system", source_run_id=None):
        body = _capability_data(data)
        actor = _string(actor, "actor", 200)
        if source_run_id is not None:
            source_run_id = _id(source_run_id, "source_run_id")
        capability_id = uuid.uuid4().hex
        at = now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO capabilities VALUES (?,?,?,?)", (capability_id, 1, at, at))
            db.execute(
                "INSERT INTO capability_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (capability_id, 1, body["name"], body["description"], body["category"],
                 body["instructions"], body["input_description"], body["output_description"],
                 _json(body["acceptance"]), body["status"], source_run_id, at),
            )
            self._audit(db, capability_id=capability_id, kind="capability.created", actor=actor,
                        payload={"revision": 1, "status": body["status"], "source_run_id": source_run_id})
        return self.get(capability_id)

    def update(self, capability_id, data, expected_revision, *, actor="system"):
        capability_id = _id(capability_id, "capability_id")
        expected_revision = _revision(expected_revision, "expected_revision")
        actor = _string(actor, "actor", 200)
        if not isinstance(data, Mapping):
            raise ValueError("capability body must be an object")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM capabilities WHERE id=?", (capability_id,)).fetchone()
            if current is None:
                raise KeyError(capability_id)
            if int(current["revision"]) != expected_revision:
                raise Conflict("能力版本已更新，请重新加载后再保存")
            prior = db.execute("SELECT * FROM capability_versions WHERE id=? AND revision=?",
                               (capability_id, expected_revision)).fetchone()
            merged = {k: prior[k] for k in ("name", "description", "category", "instructions",
                                             "input_description", "output_description", "status")}
            merged["acceptance"] = json.loads(prior["acceptance"])
            merged.update(dict(data))
            body = _capability_data(merged)
            revision = expected_revision + 1
            at = now()
            db.execute(
                "INSERT INTO capability_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (capability_id, revision, body["name"], body["description"], body["category"],
                 body["instructions"], body["input_description"], body["output_description"],
                 _json(body["acceptance"]), body["status"], prior["source_run_id"], at),
            )
            db.execute("UPDATE capabilities SET revision=?,updated_at=? WHERE id=?",
                       (revision, at, capability_id))
            self._audit(db, capability_id=capability_id, kind="capability.updated", actor=actor,
                        payload={"revision": revision, "previous_revision": expected_revision,
                                 "status": body["status"]})
        return self.get(capability_id)

    def bind(self, project_id, capability_id, revision, *, actor="system"):
        project_id = _id(project_id, "project_id")
        capability_id = _id(capability_id, "capability_id")
        revision = _revision(revision)
        actor = _string(actor, "actor", 200)
        self.store.project(project_id)
        capability = self.get(capability_id, revision)
        if capability["status"] != "ready":
            raise Conflict("草稿或未就绪能力不能绑定到项目")
        at = now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO capability_bindings(project_id,capability_id,revision,actor,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,capability_id) DO UPDATE SET revision=excluded.revision,actor=excluded.actor,updated_at=excluded.updated_at",
                (project_id, capability_id, revision, actor, at, at),
            )
            self._audit(db, capability_id=capability_id, project_id=project_id,
                        kind="capability.bound", actor=actor,
                        payload={"revision": revision})
        return {"capability_id": capability_id, "revision": revision,
                "name": capability["name"], "status": capability["status"]}

    def bindings(self, project_id):
        project_id = _id(project_id, "project_id")
        self.store.project(project_id)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT b.capability_id,b.revision,v.name,v.status FROM capability_bindings b "
                "JOIN capability_versions v ON v.id=b.capability_id AND v.revision=b.revision "
                "WHERE b.project_id=? ORDER BY b.capability_id", (project_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def unbind(self, project_id, capability_id, *, actor="system"):
        project_id = _id(project_id, "project_id")
        capability_id = _id(capability_id, "capability_id")
        actor = _string(actor, "actor", 200)
        self.store.project(project_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM capability_bindings WHERE project_id=? AND capability_id=?",
                             (project_id, capability_id)).fetchone()
            if row is None:
                raise KeyError(capability_id)
            db.execute("DELETE FROM capability_bindings WHERE project_id=? AND capability_id=?",
                       (project_id, capability_id))
            self._audit(db, capability_id=capability_id, project_id=project_id,
                        kind="capability.unbound", actor=actor,
                        payload={"revision": int(row["revision"])})
        return {"ok": True, "capability_id": capability_id}

    def invocation(self, project_id, capability_id, revision, request, *, actor):
        project_id = _id(project_id, "project_id")
        capability_id = _id(capability_id, "capability_id")
        revision = _revision(revision)
        request = _string(request, "request", 50000)
        actor = _string(actor, "actor", 200)
        self.store.project(project_id)
        capability = self.get(capability_id, revision)
        if capability["status"] != "ready":
            raise Conflict("草稿或未就绪能力不能调用")
        with self.store.connect() as db:
            binding = db.execute("SELECT revision FROM capability_bindings WHERE project_id=? AND capability_id=?",
                                 (project_id, capability_id)).fetchone()
        if binding is None:
            raise Conflict("能力尚未绑定到该项目")
        if int(binding["revision"]) != revision:
            raise Conflict("调用版本与项目绑定版本不一致")
        # The snapshot is returned separately so the caller can put it in the
        # run before dispatch.  The provenance fields make authorization facts
        # visible without claiming a provider or a live execution result.
        return {**capability, "source": {"type": "capability", "capability_id": capability_id,
                                          "capability_revision": revision, "actor": actor},
                "request": request}

    def distill(self, run, data, *, actor="system"):
        if not isinstance(run, Mapping):
            raise ValueError("run must be an object")
        if run.get("status") not in {"ready_for_review", "published"}:
            raise Conflict("只有已验证或已发布的运行才能提炼能力候选")
        if not isinstance(data, Mapping):
            raise ValueError("distill body must be an object")
        name = _string(data.get("name"), "name", 120)
        description = _string(data.get("description", ""), "description", 4000, required=False)
        category = data.get("category", "custom")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError("invalid capability category")
        request = _string(run.get("request", ""), "run.request", 50000, required=False)
        plan = run.get("plan") or {}
        tasks = plan.get("tasks") if isinstance(plan, Mapping) else []
        if not isinstance(tasks, list):
            tasks = []
        task_lines = []
        acceptance = []
        for task in tasks[:20]:
            if not isinstance(task, Mapping):
                continue
            title = _string(task.get("title", task.get("id", "任务")), "task title", 300, required=False)
            task_lines.append(title)
            values = task.get("acceptance", [])
            if isinstance(values, list):
                acceptance.extend(x for x in values if isinstance(x, str) and x.strip())
        acceptance = [_string(x, "acceptance item", 2000) for x in acceptance[:20]]
        evidence = scrub(run.get("artifacts") or {})
        instructions = (
            "确定性候选材料，需人工审阅后配置为可调用能力。\n"
            f"来源运行：{run.get('id', '')}\n需求：{request}\n"
            f"计划任务：{'；'.join(task_lines) if task_lines else '未记录结构化任务'}\n"
            f"交付证据：{json.dumps(evidence, ensure_ascii=False, sort_keys=True)[:5000]}"
        )
        return self.create({
            "name": name, "description": description or "从已验证运行确定性提炼的候选能力，待人工审阅。",
            "category": category, "instructions": instructions[:12000],
            "input_description": "运行中记录的原始需求，调用前需确认范围和授权。",
            "output_description": "对应运行的计划、检查和交付证据；候选能力不代表已验证的全局事实。",
            "acceptance": acceptance, "status": "draft",
        }, actor=actor, source_run_id=run.get("id"))
