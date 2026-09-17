"""Versioned capability packs: development output → candidate → evaluation → immutable
published version → agent binding → isolated invocation.

Distinct from `capabilities.py` (distilled *methods* bound to projects) and from agent
versions (a role's configuration). A pack is an **executable capability**: program files,
a declared tool contract, a dependency lock, a support matrix and the evaluation policy
that decides whether it may be published at all.

Three states are kept apart on purpose and never collapsed into one green "ready":
version lifecycle, environment availability, and a single execution's result. A file that
was produced but failed validation is a *failed* execution whose candidate output is still
downloadable and labelled unverified.

Client material and task results never enter a pack by default. Only artifacts the caller
explicitly selects are considered, fixtures additionally require a declared material scope,
and everything excluded is reported with its reason.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
import zipfile
from pathlib import Path, PurePosixPath

from factory.control.store import Conflict, now, scrub

SCHEMA = 'webuddy.capability-pack/v1'
MANIFEST_NAME = 'webuddy-pack.json'
LIFECYCLE = ('draft', 'building', 'validating', 'verified', 'published', 'retired', 'needs_changes')
ENV_STATUS = ('unchecked', 'checking', 'ready', 'unavailable')
TASK_STATUS = ('queued', 'running', 'waiting_input', 'cancel_requested', 'succeeded', 'failed', 'cancelled')
MATERIAL_SCOPES = ('synthetic', 'licensed')
# Directories that hold the customer's own material in a development workspace. Anything
# under them is client material by default, whatever the caller selected.
CLIENT_DIRS = {'input', 'inputs', 'material', 'materials', 'upload', 'uploads', 'raw', 'private', '客户资料', '原始资料'}
# Deliverable extensions that are a *task result* for one customer, not a reusable program.
RESULT_SUFFIXES = {'.xml', '.xlsx', '.docx', '.pdf', '.mfd', '.zip'}
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PACK_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def content_digest(files) -> str:
    """Identity of a candidate's content: path + bytes, order-independent.

    Publishing re-checks this digest inside the transaction, and evidence is bound to it,
    so a candidate edited after validation can never be published on the old evidence.
    """
    parts = ''.join(f"{f['path']}\0{f['sha256']}\n" for f in sorted(files, key=lambda f: f['path']))
    return hashlib.sha256(parts.encode()).hexdigest()


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and len(name) <= 255 and '\\' not in name and ':' not in name and not path.is_absolute() \
        and '..' not in path.parts and not any(ord(c) < 32 for c in name) \
        and not any(p.startswith('.') and p not in {'.gitignore'} for p in path.parts)


def classify_selection(name, kind, *, declared_scope=None):
    """Why a selected artifact may or may not enter a reusable pack.

    Returns (role, reason). role is 'program' | 'fixture' | None; reason is the honest
    explanation shown to the user when the artifact is excluded.
    """
    parts = [p.casefold() for p in PurePosixPath(name).parts]
    if not safe_member(name):
        return None, '路径不合法或指向隐藏/越界文件'
    if kind in ('installer', 'package'):
        return None, '安装包与压缩产物不入包；职能包保存程序与方法，不保存发行物'
    if any(p in CLIENT_DIRS for p in parts[:-1]):
        return None, '客户原始资料默认不入包'
    suffix = PurePosixPath(name).suffix.casefold()
    is_test_area = any(p in ('fixtures', 'tests', 'testdata', 'samples') for p in parts[:-1])
    if suffix in RESULT_SUFFIXES or is_test_area:
        if not is_test_area:
            return None, '任务结果默认不入包；如需作为测试样本，请放入 fixtures/ 并声明资料范围'
        if declared_scope not in MATERIAL_SCOPES:
            return None, '测试样本须声明资料范围（synthetic 合成 / licensed 已获授权），未声明不入包'
        return 'fixture', ''
    return 'program', ''


def validate_manifest(raw, paths):
    """The tool contract the development run produced. Fail closed: a pack without a
    complete, self-consistent contract is never publishable, and we say which part is missing."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f'{MANIFEST_NAME} 不是合法 JSON') from None
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError(f'{MANIFEST_NAME} 缺少 schema="{SCHEMA}"')
    name = str(data.get('name') or '').strip()
    purpose = str(data.get('purpose') or '').strip()
    if not 1 <= len(name) <= 120:
        raise ValueError('name 必须是 1–120 字')
    if not 1 <= len(purpose) <= 4000:
        raise ValueError('purpose 必须说明这个能力做什么')
    tool = data.get('tool')
    if not isinstance(tool, dict):
        raise ValueError('缺少 tool 契约')
    tool_name = str(tool.get('name') or '')
    if not tool_name.replace('_', '').isalnum() or not 1 <= len(tool_name) <= 60:
        raise ValueError('tool.name 只能是字母数字下划线')
    entrypoint = str(tool.get('entrypoint') or '')
    if entrypoint not in paths:
        raise ValueError('tool.entrypoint 必须指向包内文件')
    if tool.get('runtime') != 'python3':
        raise ValueError('当前只支持 runtime="python3"')
    timeout = tool.get('timeout_seconds', 60)
    if not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise ValueError('tool.timeout_seconds 必须是 1–600 秒')
    for field in ('input_schema', 'output_schema'):
        if not isinstance(tool.get(field), dict) or not tool[field]:
            raise ValueError(f'tool.{field} 必须是非空 JSON schema 对象')
    permissions = tool.get('permissions') or {}
    if not isinstance(permissions, dict):
        raise ValueError('tool.permissions 必须是对象')
    if permissions.get('network'):
        raise ValueError('业务调用阶段不联网：tool.permissions.network 必须为 false')
    lock = data.get('dependency_lock')
    if not isinstance(lock, dict) or not str(lock.get('python') or '').strip():
        raise ValueError('缺少 dependency_lock.python')
    packages = lock.get('packages', [])
    if not isinstance(packages, list) or any(not isinstance(p, str) for p in packages):
        raise ValueError('dependency_lock.packages 必须是字符串数组')
    matrix = data.get('support_matrix')
    if not isinstance(matrix, list) or not matrix:
        raise ValueError('support_matrix 必须写明已验证与未验证的格式范围')
    for row in matrix:
        if not isinstance(row, dict) or not str(row.get('format') or '').strip() \
                or row.get('status') not in ('supported', 'partial', 'unsupported'):
            raise ValueError('support_matrix 每项需要 format 与 status(supported/partial/unsupported)')
    policy = data.get('evaluation_policy')
    if not isinstance(policy, dict) or str(policy.get('test_set') or '') not in paths:
        raise ValueError('evaluation_policy.test_set 必须指向包内测试集文件')
    return {'schema': SCHEMA, 'name': name, 'purpose': purpose,
            'tool': {'name': tool_name, 'entrypoint': entrypoint, 'runtime': 'python3',
                     'timeout_seconds': timeout, 'input_schema': tool['input_schema'],
                     'output_schema': tool['output_schema'],
                     'permissions': {'network': False,
                                     'max_input_bytes': int(permissions.get('max_input_bytes') or MAX_ARTIFACT_BYTES),
                                     'max_output_bytes': int(permissions.get('max_output_bytes') or MAX_ARTIFACT_BYTES)}},
            'dependency_lock': {'python': str(lock['python']), 'packages': [str(p) for p in packages]},
            'support_matrix': [{'format': str(r['format']), 'status': r['status'],
                                'evidence': str(r.get('evidence') or '')} for r in matrix],
            'evaluation_policy': {'test_set': policy['test_set'], 'required': bool(policy.get('required', True))}}


class PackStore:
    """Additive tables; existing rows are never rewritten. Published versions and
    evaluation evidence are immutable at the database level, not by convention."""

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capability_packs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pack_drafts(pack_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pack_blobs(sha256 TEXT PRIMARY KEY, content BLOB NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS pack_versions(id TEXT PRIMARY KEY, pack_id TEXT NOT NULL,
                    version INTEGER NOT NULL, content_digest TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(pack_id, version));
                CREATE TRIGGER IF NOT EXISTS no_pack_version_update BEFORE UPDATE ON pack_versions
                    BEGIN SELECT RAISE(ABORT,'published pack versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_pack_version_delete BEFORE DELETE ON pack_versions
                    BEGIN SELECT RAISE(ABORT,'published pack versions are immutable'); END;
                CREATE TABLE IF NOT EXISTS pack_evaluations(id TEXT PRIMARY KEY, pack_id TEXT NOT NULL,
                    content_digest TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_pack_evaluation_update BEFORE UPDATE ON pack_evaluations
                    BEGIN SELECT RAISE(ABORT,'evaluation evidence is append-only'); END;
                CREATE TABLE IF NOT EXISTS pack_env_checks(version_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pack_bindings(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                    pack_id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(agent_id, pack_id));
                CREATE TABLE IF NOT EXISTS pack_tasks(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pack_task_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS pack_task_events_task ON pack_task_events(task_id, id);
                CREATE TRIGGER IF NOT EXISTS no_pack_event_update BEFORE UPDATE ON pack_task_events
                    BEGIN SELECT RAISE(ABORT,'pack task events are append-only'); END;
                CREATE TABLE IF NOT EXISTS pack_artifacts(id TEXT PRIMARY KEY, data TEXT NOT NULL, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS pack_operations(actor_id TEXT NOT NULL, operation_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, operation_key));
            ''')

    # ---- idempotency -------------------------------------------------------
    @staticmethod
    def _fingerprint(payload) -> str:
        return hashlib.sha256(_json(payload).encode()).hexdigest()

    def _replay(self, db, actor_id, key, fingerprint):
        """Same actor + same key: replay the first result. Same key, different input: 409.
        A lost response can therefore never create a second draft, version or task."""
        if not key:
            return None
        row = db.execute('SELECT fingerprint,result FROM pack_operations WHERE actor_id=? AND operation_key=?',
                         (str(actor_id), key)).fetchone()
        if row is None:
            return None
        if row['fingerprint'] != fingerprint:
            raise Conflict('同一操作键对应了不同的请求内容')
        return json.loads(row['result'])

    def _record(self, db, actor_id, key, fingerprint, result):
        if key:
            db.execute('INSERT OR REPLACE INTO pack_operations VALUES(?,?,?,?,?)',
                       (str(actor_id), key, fingerprint, _json(result), now()))
        return result

    # ---- reads -------------------------------------------------------------
    def _pack_row(self, db, pack_id):
        row = db.execute('SELECT data FROM capability_packs WHERE id=?', (pack_id,)).fetchone()
        if row is None:
            raise KeyError(pack_id)
        return json.loads(row['data'])

    def _require_maintainer(self, pack, actor):
        if str(pack['owner_id']) != str(actor['id']) and actor.get('role') != 'admin':
            raise PermissionError('只有能力维护者可以修改该职能包')

    def get(self, pack_id):
        with self.store.connect() as db:
            pack = self._pack_row(db, pack_id)
            draft = db.execute('SELECT data FROM pack_drafts WHERE pack_id=?', (pack_id,)).fetchone()
            versions = [self._version_view(r) for r in db.execute(
                'SELECT * FROM pack_versions WHERE pack_id=? ORDER BY version DESC', (pack_id,))]
            evaluations = [{**json.loads(r['data']), 'id': r['id'], 'content_digest': r['content_digest'],
                            'created_at': r['created_at']} for r in db.execute(
                'SELECT * FROM pack_evaluations WHERE pack_id=? ORDER BY created_at DESC', (pack_id,))]
            bindings = [{**json.loads(r['data']), 'id': r['id'], 'agent_id': r['agent_id']} for r in db.execute(
                'SELECT * FROM pack_bindings WHERE pack_id=?', (pack_id,))]
            environments = {v['id']: self._env(db, v['id']) for v in versions}
        body = json.loads(draft['data']) if draft else None
        if body is not None:
            # evidence for the *current* candidate only; older evidence is kept but not credited
            body['evaluations'] = [e for e in evaluations if e['content_digest'] == body['content_digest']]
        return {**pack, 'draft': body, 'versions': versions, 'evaluations': evaluations,
                'bindings': bindings, 'environments': environments}

    @staticmethod
    def _version_view(row):
        return {**json.loads(row['data']), 'id': row['id'], 'pack_id': row['pack_id'],
                'version': row['version'], 'content_digest': row['content_digest'], 'created_at': row['created_at']}

    def version(self, version_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM pack_versions WHERE id=?', (version_id,)).fetchone()
            if row is None:
                raise KeyError(version_id)
            return self._version_view(row)

    def list(self):
        with self.store.connect() as db:
            packs = [json.loads(r['data']) for r in db.execute('SELECT data FROM capability_packs ORDER BY rowid DESC')]
            published = {}
            for r in db.execute('SELECT pack_id, MAX(version) AS v FROM pack_versions GROUP BY pack_id'):
                published[r['pack_id']] = r['v']
        return [{**p, 'published_version': published.get(p['id'])} for p in packs]

    def files(self, *, pack_id=None, version_id=None):
        with self.store.connect() as db:
            if version_id:
                entries = self.version(version_id)['files']
            else:
                row = db.execute('SELECT data FROM pack_drafts WHERE pack_id=?', (pack_id,)).fetchone()
                if row is None:
                    raise KeyError(pack_id)
                entries = json.loads(row['data'])['files']
            out = {}
            for entry in entries:
                blob = db.execute('SELECT content FROM pack_blobs WHERE sha256=?', (entry['sha256'],)).fetchone()
                if blob is None:
                    raise Conflict('包内容缺失，无法读取')
                out[entry['path']] = blob['content']
        return out

    ENV_CHECK_TTL_SECONDS = 24 * 3600

    @staticmethod
    def _runtime_fingerprint():
        import platform
        return f'{platform.python_version()}|{platform.platform()}'

    def _env(self, db, version_id):
        """环境检查会过期。

        解释器换了、平台换了、或者检查已经过了 TTL，就退回 unchecked 并说明原因——
        依赖是会变的，一次 ready 不能永久代表「现在能跑」。
        """
        row = db.execute('SELECT data FROM pack_env_checks WHERE version_id=?', (version_id,)).fetchone()
        if not row:
            return {'status': 'unchecked'}
        body = json.loads(row['data'])
        reason = None
        if body.get('runtime_fingerprint') != self._runtime_fingerprint():
            reason = '运行环境已变化，上次检查结果不再适用'
        else:
            try:
                checked = datetime.fromisoformat(body['checked_at'])
                if (datetime.now(timezone.utc) - checked).total_seconds() > self.ENV_CHECK_TTL_SECONDS:
                    reason = '上次检查已超过 24 小时'
            except (KeyError, ValueError):
                reason = '上次检查时间无法解析'
        if reason:
            return {**body, 'status': 'unchecked', 'stale': True, 'stale_reason': reason}
        return {**body, 'stale': False}

    # ---- candidate ---------------------------------------------------------
    def create_draft(self, *, source, selections, actor, operation_key=None, name=None, purpose=None):
        """Build a candidate from *selected* development artifacts.

        `source` is {'kind':'run','id':rid,'items':[{name,kind,sha256,size,content}]} — the
        caller resolves the run's saved deliverables; this layer decides what may enter a
        reusable pack and records every exclusion with its reason.
        """
        chosen = {str(s['name']): s for s in selections}
        fingerprint = self._fingerprint({'source': source['id'], 'selections': sorted(chosen)})
        files, excluded = [], []
        for item in source['items']:
            pick = chosen.get(item['name'])
            if pick is None:
                continue
            role, reason = classify_selection(item['name'], item.get('kind', 'source'),
                                              declared_scope=pick.get('material_scope'))
            if role is None:
                excluded.append({'path': item['name'], 'reason': reason})
                continue
            if item['size'] > MAX_FILE_BYTES:
                excluded.append({'path': item['name'], 'reason': f'单文件超过 {MAX_FILE_BYTES // 1024} KB'})
                continue
            files.append({'path': item['name'], 'sha256': item['sha256'], 'size': item['size'],
                          'role': role, 'material_scope': pick.get('material_scope'), 'content': item['content']})
        missing = [str(n) for n in chosen if n not in {i['name'] for i in source['items']}]
        excluded += [{'path': n, 'reason': '不在该任务的成果清单内'} for n in missing]
        if not files:
            raise ValueError('没有可进入职能包的产物：' + ('；'.join(e['reason'] for e in excluded) or '未选择任何文件'))
        if len(files) > MAX_FILES or sum(f['size'] for f in files) > MAX_PACK_BYTES:
            raise ValueError('候选内容超过上限（200 个文件 / 8 MB）')
        paths = {f['path'] for f in files}
        manifest, blocked = None, None
        if MANIFEST_NAME not in paths:
            blocked = f'缺少 {MANIFEST_NAME} 工具契约，无法验证或发布'
        else:
            try:
                manifest = validate_manifest(next(f['content'] for f in files if f['path'] == MANIFEST_NAME), paths)
            except ValueError as exc:
                blocked = str(exc)
        entries = [{k: v for k, v in f.items() if k != 'content'} for f in files]
        digest = content_digest(entries)
        pack_id = uuid.uuid4().hex
        record = {'id': pack_id, 'name': (manifest or {}).get('name') or (name or '未命名能力'),
                  'purpose': (manifest or {}).get('purpose') or (purpose or ''),
                  'owner_id': str(actor['id']), 'source_task_id': source['id'], 'source_kind': source['kind'],
                  'created_at': now(), 'updated_at': now(), 'revision': 1}
        draft = {'pack_id': pack_id, 'revision': 1, 'lifecycle': 'draft' if manifest else 'needs_changes',
                 'blocked_reason': blocked, 'manifest': manifest, 'files': entries, 'content_digest': digest,
                 'excluded': excluded, 'source_task_id': source['id'], 'updated_at': now()}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replay(db, actor['id'], operation_key, fingerprint)
            if replay is not None:
                return replay
            for f in files:
                db.execute('INSERT OR IGNORE INTO pack_blobs VALUES(?,?,?)', (f['sha256'], f['content'], f['size']))
            db.execute('INSERT INTO capability_packs VALUES(?,?)', (pack_id, _json(record)))
            db.execute('INSERT INTO pack_drafts VALUES(?,?,?)', (pack_id, _json(draft), now()))
            return self._record(db, actor['id'], operation_key, fingerprint, {**record, 'draft': draft})

    def update_draft(self, pack_id, *, expected_revision, files, actor):
        """Replace candidate content under an optimistic lock. Changing the content changes
        the digest, which invalidates every evaluation bound to the old one."""
        entries, blobs = [], []
        for f in files:
            content = f['content'] if isinstance(f['content'], bytes) else str(f['content']).encode()
            if not safe_member(f['path']):
                raise ValueError(f"路径不合法：{f['path']}")
            if len(content) > MAX_FILE_BYTES:
                raise ValueError(f"单文件超过上限：{f['path']}")
            digest = hashlib.sha256(content).hexdigest()
            entries.append({'path': f['path'], 'sha256': digest, 'size': len(content),
                            'role': f.get('role', 'program'), 'material_scope': f.get('material_scope')})
            blobs.append((digest, content, len(content)))
        if not entries or len(entries) > MAX_FILES or sum(e['size'] for e in entries) > MAX_PACK_BYTES:
            raise ValueError('候选内容为空或超过上限（200 个文件 / 8 MB）')
        paths = {e['path'] for e in entries}
        manifest, blocked = None, None
        if MANIFEST_NAME not in paths:
            blocked = f'缺少 {MANIFEST_NAME} 工具契约，无法验证或发布'
        else:
            raw = next(c for d, c, _ in blobs if d == next(e['sha256'] for e in entries if e['path'] == MANIFEST_NAME))
            try:
                manifest = validate_manifest(raw, paths)
            except ValueError as exc:
                blocked = str(exc)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pack = self._pack_row(db, pack_id)
            self._require_maintainer(pack, actor)
            row = db.execute('SELECT data FROM pack_drafts WHERE pack_id=?', (pack_id,)).fetchone()
            if row is None:
                raise KeyError(pack_id)
            draft = json.loads(row['data'])
            if int(expected_revision) != int(draft['revision']):
                raise Conflict('候选内容已被更新，请刷新后重试')
            for blob in blobs:
                db.execute('INSERT OR IGNORE INTO pack_blobs VALUES(?,?,?)', blob)
            draft = {**draft, 'revision': draft['revision'] + 1, 'files': entries,
                     'content_digest': content_digest(entries), 'manifest': manifest,
                     'blocked_reason': blocked, 'lifecycle': 'draft' if manifest else 'needs_changes',
                     'updated_at': now()}
            db.execute('UPDATE pack_drafts SET data=?,updated_at=? WHERE pack_id=?', (_json(draft), now(), pack_id))
            pack = {**pack, 'updated_at': now(), 'revision': pack['revision'] + 1,
                    'name': (manifest or {}).get('name') or pack['name'],
                    'purpose': (manifest or {}).get('purpose') or pack['purpose']}
            db.execute('UPDATE capability_packs SET data=? WHERE id=?', (_json(pack), pack_id))
        return {**pack, 'draft': draft}

    def draft(self, pack_id):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM pack_drafts WHERE pack_id=?', (pack_id,)).fetchone()
            if row is None:
                raise KeyError(pack_id)
            return json.loads(row['data'])

    # ---- evidence ----------------------------------------------------------
    def save_evaluation(self, pack_id, *, content_digest_value, report, actor, operation_key=None):
        """Evidence is append-only and bound to the exact candidate digest it ran against."""
        # 与 begin_evaluation 用同一个指纹口径：提交时已登记，这里只是把结果补进去。
        fingerprint = self._fingerprint({'kind': 'evaluation', 'pack': pack_id,
                                         'digest': content_digest_value})
        eid = uuid.uuid4().hex
        body = {'passed': bool(report.get('passed')), 'summary': scrub(report.get('summary', ''))[:2000],
                'cases': scrub(report.get('cases', [])), 'environment': scrub(report.get('environment', {})),
                'test_set': report.get('test_set'), 'actor_id': str(actor['id']), 'scope': report.get('scope', [])}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pack = self._pack_row(db, pack_id)
            self._require_maintainer(pack, actor)
            row = db.execute('SELECT data FROM pack_drafts WHERE pack_id=?', (pack_id,)).fetchone()
            draft = json.loads(row['data']) if row else None
            db.execute('INSERT INTO pack_evaluations VALUES(?,?,?,?,?)',
                       (eid, pack_id, content_digest_value, _json(body), now()))
            if draft and draft['content_digest'] == content_digest_value:
                draft = {**draft, 'lifecycle': 'verified' if body['passed'] else 'needs_changes',
                         'blocked_reason': None if body['passed'] else '验证未通过，请继续修复', 'updated_at': now()}
                db.execute('UPDATE pack_drafts SET data=?,updated_at=? WHERE pack_id=?', (_json(draft), now(), pack_id))
            result = {**body, 'id': eid, 'pack_id': pack_id, 'content_digest': content_digest_value, 'created_at': now()}
            return self._record(db, actor['id'], operation_key, fingerprint, result)

    # ---- publish -----------------------------------------------------------
    def publish(self, pack_id, *, expected_revision, evaluation_id, actor, operation_key=None):
        """Every publish condition is re-checked inside the transaction: maintainer rights,
        the candidate digest still matching the evidence, a passing evaluation, and a
        complete tool contract. `verified` alone is not a bypass."""
        fingerprint = self._fingerprint({'pack': pack_id, 'revision': expected_revision, 'evaluation': evaluation_id})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pack = self._pack_row(db, pack_id)
            self._require_maintainer(pack, actor)
            # 重放要在候选 revision 检查**之前**：发布本身会把 revision +1，
            # 丢响应后的重试拿着原来的 expected_revision，否则会被误判成「内容已被更新」。
            replay = self._replay(db, actor['id'], operation_key, fingerprint)
            if replay is not None:
                return replay
            row = db.execute('SELECT data FROM pack_drafts WHERE pack_id=?', (pack_id,)).fetchone()
            if row is None:
                raise KeyError(pack_id)
            draft = json.loads(row['data'])
            if int(expected_revision) != int(draft['revision']):
                raise Conflict('候选内容已被更新，请重新验证后发布')
            if not draft.get('manifest'):
                raise Conflict(draft.get('blocked_reason') or '工具契约不完整，无法发布')
            evaluation = db.execute('SELECT * FROM pack_evaluations WHERE id=? AND pack_id=?',
                                    (evaluation_id, pack_id)).fetchone()
            if evaluation is None:
                raise KeyError(evaluation_id)
            evidence = json.loads(evaluation['data'])
            if evaluation['content_digest'] != draft['content_digest']:
                raise Conflict('验证记录对应的候选内容已被修改，请重新运行验证')
            if not evidence.get('passed'):
                raise Conflict('该验证未通过，不能发布')
            latest = db.execute('SELECT MAX(version) AS v FROM pack_versions WHERE pack_id=?', (pack_id,)).fetchone()
            number = int(latest['v'] or 0) + 1
            version_id = uuid.uuid4().hex
            body = {'manifest': draft['manifest'], 'files': draft['files'], 'evaluation_id': evaluation_id,
                    'source_task_id': draft.get('source_task_id'), 'published_by': str(actor['id']),
                    'support_matrix': draft['manifest']['support_matrix'],
                    'excluded': draft.get('excluded', []), 'lifecycle': 'published'}
            db.execute('INSERT INTO pack_versions VALUES(?,?,?,?,?,?)',
                       (version_id, pack_id, number, draft['content_digest'], _json(body), now()))
            # The draft revision advances on publish too, so a second concurrent publish
            # holding the same expected_revision is refused rather than cutting version N+2.
            db.execute('UPDATE pack_drafts SET data=?,updated_at=? WHERE pack_id=?',
                       (_json({**draft, 'revision': draft['revision'] + 1, 'lifecycle': 'published',
                               'published_version_id': version_id}), now(), pack_id))
            db.execute('UPDATE capability_packs SET data=? WHERE id=?',
                       (_json({**pack, 'updated_at': now(), 'published_version': number}), pack_id))
            result = {'id': version_id, 'pack_id': pack_id, 'version': number,
                      'content_digest': draft['content_digest'], 'created_at': now(), **body}
            return self._record(db, actor['id'], operation_key, fingerprint, result)

    # ---- environment -------------------------------------------------------
    def record_env_check(self, version_id, report):
        if report.get('status') not in ENV_STATUS:
            raise ValueError('环境状态不合法')
        version = self.version(version_id)
        body = {**report, 'version_id': version_id, 'content_digest': version['content_digest'],
                'runtime_fingerprint': self._runtime_fingerprint(), 'checked_at': now()}
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO pack_env_checks VALUES(?,?)', (version_id, _json(body)))
        return body

    # ---- binding -----------------------------------------------------------
    def bind(self, agent_id, version_id, *, expected_revision, actor, allowed_uses=None):
        """A binding pins one concrete version. Upgrading only changes which version FUTURE
        tasks freeze; running tasks and existing conversations keep their snapshot."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version_row = db.execute('SELECT * FROM pack_versions WHERE id=?', (version_id,)).fetchone()
            if version_row is None:
                raise KeyError(version_id)
            pack = self._pack_row(db, version_row['pack_id'])
            if actor.get('role') != 'admin' and str(pack['owner_id']) != str(actor['id']):
                raise PermissionError('只有能力维护者可以挂靠或升级该职能包')
            existing = db.execute('SELECT * FROM pack_bindings WHERE agent_id=? AND pack_id=?',
                                  (agent_id, version_row['pack_id'])).fetchone()
            current = json.loads(existing['data']) if existing else None
            if int(expected_revision) != int(current['revision'] if current else 0):
                raise Conflict('挂靠已被他人更新，请刷新后重试')
            body = {'pack_id': version_row['pack_id'], 'pack_name': pack['name'], 'version_id': version_id,
                    'version': version_row['version'], 'content_digest': version_row['content_digest'],
                    'revision': (current['revision'] + 1) if current else 1,
                    'allowed_uses': list(allowed_uses or ['invoke']), 'actor_id': str(actor['id']),
                    'created_at': current['created_at'] if current else now(), 'updated_at': now()}
            if existing:
                db.execute('UPDATE pack_bindings SET data=? WHERE id=?', (_json(body), existing['id']))
                bid = existing['id']
            else:
                bid = uuid.uuid4().hex
                db.execute('INSERT INTO pack_bindings VALUES(?,?,?,?)', (bid, agent_id, version_row['pack_id'], _json(body)))
        return {**body, 'id': bid, 'agent_id': agent_id}

    def unbind(self, agent_id, pack_id, *, actor):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pack = self._pack_row(db, pack_id)
            self._require_maintainer(pack, actor)
            db.execute('DELETE FROM pack_bindings WHERE agent_id=? AND pack_id=?', (agent_id, pack_id))
        return {'agent_id': agent_id, 'pack_id': pack_id, 'bound': False}

    def bindings(self, agent_id):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM pack_bindings WHERE agent_id=?', (agent_id,)).fetchall()
            out = []
            for row in rows:
                body = json.loads(row['data'])
                latest = db.execute('SELECT MAX(version) AS v FROM pack_versions WHERE pack_id=?',
                                    (row['pack_id'],)).fetchone()
                # Expose the tool contract so the UI can render input constraints
                # and support scope from the manifest, not from hardcoded assumptions.
                ver_row = db.execute('SELECT data FROM pack_versions WHERE id=?',
                                     (body['version_id'],)).fetchone()
                tool_contract = None
                if ver_row:
                    ver_data = json.loads(ver_row['data'])
                    manifest = ver_data.get('manifest') or {}
                    tool = manifest.get('tool') or {}
                    tool_contract = {
                        'permissions': tool.get('permissions'),
                        'input_schema': tool.get('input_schema'),
                        'output_schema': tool.get('output_schema'),
                        'timeout_seconds': tool.get('timeout_seconds'),
                        'support_matrix': manifest.get('support_matrix'),
                        'purpose': manifest.get('purpose'),
                    }
                out.append({**body, 'id': row['id'], 'agent_id': agent_id,
                            'latest_version': int(latest['v'] or 0),
                            'upgrade_available': int(latest['v'] or 0) > int(body['version']),
                            'environment': self._env(db, body['version_id']),
                            'tool_contract': tool_contract})
        return out

    def binding_for(self, agent_id, pack_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM pack_bindings WHERE agent_id=? AND pack_id=?', (agent_id, pack_id)).fetchone()
            if row is None:
                raise KeyError(pack_id)
            return {**json.loads(row['data']), 'id': row['id'], 'agent_id': agent_id}

    # ---- artifacts ---------------------------------------------------------
    def put_artifact(self, *, actor_id, name, content, role, task_id=None, validation_status='unverified',
                     kind='file', dedupe=False):
        """`dedupe` makes an input artifact content-addressed per owner: re-uploading the
        same bytes after a lost response returns the same artifact, so the retry replays
        one task instead of forking a second one."""
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError('文件超过 16 MB 上限')
        digest = hashlib.sha256(content).hexdigest()
        if dedupe:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT data FROM pack_artifacts WHERE json_extract(data,'$.sha256')=? "
                    "AND json_extract(data,'$.actor_id')=? AND json_extract(data,'$.role')=? "
                    "AND json_extract(data,'$.name')=? LIMIT 1",
                    (digest, str(actor_id), role, str(name)[:255])).fetchone()
                if row is not None:
                    return json.loads(row['data'])
        aid = uuid.uuid4().hex
        body = {'id': aid, 'actor_id': str(actor_id), 'task_id': task_id, 'role': role, 'kind': kind,
                'name': str(name)[:255], 'size': len(content), 'sha256': digest,
                'validation_status': validation_status, 'at': now()}
        with self.store.connect() as db:
            db.execute('INSERT INTO pack_artifacts VALUES(?,?,?)', (aid, _json(body), content))
        return body

    def artifact(self, artifact_id, *, actor, with_content=False):
        """Ownership is checked here, not at the edge: a task can never read another
        user's artifact by id, whoever asked for it."""
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM pack_artifacts WHERE id=?', (artifact_id,)).fetchone()
            if row is None:
                raise KeyError(artifact_id)
            body = json.loads(row['data'])
            if str(body['actor_id']) != str(actor['id']) and actor.get('role') != 'admin':
                raise PermissionError('无权访问该文件')
            return (body, row['content']) if with_content else body

    # ---- tasks -------------------------------------------------------------
    @staticmethod
    def _agent_version(db, agent_id):
        """角色版本在**同一个事务里**读，冻结才是原子的。

        事务外先读再写的话，两次读之间落地的一次角色升级会让快照记下一个这次调用
        其实没有用到的版本号。"""
        row = db.execute('SELECT data FROM agents WHERE id=?', (agent_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row['data']).get('active_version')

    def create_task(self, *, actor, agent_id, pack_id, input_artifact_ids, options=None,
                    operation_key=None, job_id=None):
        """Freeze the capability snapshot inside the same transaction that registers the
        task, so an upgrade landing a millisecond later cannot change what this task runs."""
        fingerprint = self._fingerprint({'agent': agent_id, 'pack': pack_id, 'inputs': sorted(input_artifact_ids)})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replay(db, actor['id'], operation_key, fingerprint)
            if replay is not None:
                return {**replay, 'idempotent_replay': True}
            row = db.execute('SELECT * FROM pack_bindings WHERE agent_id=? AND pack_id=?', (agent_id, pack_id)).fetchone()
            if row is None:
                raise Conflict('该职能体尚未挂靠此能力')
            binding = json.loads(row['data'])
            version_row = db.execute('SELECT * FROM pack_versions WHERE id=?', (binding['version_id'],)).fetchone()
            if version_row is None:
                raise Conflict('挂靠的版本已不可用')
            inputs = []
            for aid in input_artifact_ids:
                art = db.execute('SELECT data FROM pack_artifacts WHERE id=?', (aid,)).fetchone()
                if art is None:
                    raise KeyError(aid)
                body = json.loads(art['data'])
                if str(body['actor_id']) != str(actor['id']) and actor.get('role') != 'admin':
                    raise PermissionError('无权使用该文件作为输入')
                inputs.append({k: body[k] for k in ('id', 'name', 'size', 'sha256')})
            env = self._env(db, binding['version_id'])
            tid = uuid.uuid4().hex
            task = {'id': tid, 'actor_id': str(actor['id']), 'agent_id': agent_id, 'pack_id': pack_id,
                    # The durable job id is written in the SAME transaction as the task, so a
                    # crash before dispatch leaves a task that recovery can recognise and a
                    # replay can re-dispatch — never a queued row nobody will ever pick up.
                    'job_id': job_id,
                    'status': 'queued', 'inputs': inputs, 'outputs': [], 'validation_status': 'pending',
                    'error_code': None, 'error': None, 'options': dict(options or {}),
                    'snapshot': {'version_id': binding['version_id'], 'version': version_row['version'],
                                 'content_digest': version_row['content_digest'],
                                 'binding_revision': binding['revision'],
                                 # The role's own version is frozen with the capability's:
                                 # both are what this task ran against, whatever changes later.
                                 'agent_version': self._agent_version(db, agent_id),
                                 'tool': json.loads(version_row['data'])['manifest']['tool']['name'],
                                 'environment': env},
                    'created_at': now(), 'updated_at': now()}
            db.execute('INSERT INTO pack_tasks VALUES(?,?)', (tid, _json(task)))
            db.execute('INSERT INTO pack_task_events(task_id,type,payload,at) VALUES(?,?,?,?)',
                       (tid, 'task.queued', _json({'version': version_row['version']}), now()))
            return self._record(db, actor['id'], operation_key, fingerprint, task)

    def task(self, task_id, *, actor=None):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM pack_tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = json.loads(row['data'])
        if actor is not None and str(task['actor_id']) != str(actor['id']) and actor.get('role') != 'admin':
            raise PermissionError('无权访问该任务')
        return task

    def update_task(self, task_id, patch, *, event=None, expected=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM pack_tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = json.loads(row['data'])
            if expected is not None and task['status'] not in expected:
                raise Conflict(f"任务状态为 {task['status']}，无法执行该变更")
            if patch.get('status') and patch['status'] not in TASK_STATUS:
                raise ValueError('执行状态不合法')
            task = {**task, **scrub(patch), 'updated_at': now()}
            db.execute('UPDATE pack_tasks SET data=? WHERE id=?', (_json(task), task_id))
            if event:
                db.execute('INSERT INTO pack_task_events(task_id,type,payload,at) VALUES(?,?,?,?)',
                           (task_id, event[0], _json(scrub(event[1])), now()))
        return task

    def task_events(self, task_id, *, cursor=0, actor=None):
        self.task(task_id, actor=actor)
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM pack_task_events WHERE task_id=? AND id>? ORDER BY id', (task_id, int(cursor)))
            events = [{'id': r['id'], 'type': r['type'], 'payload': json.loads(r['payload']), 'at': r['at']} for r in rows]
        return {'events': events, 'cursor': events[-1]['id'] if events else int(cursor)}

    def recover_interrupted(self, *, job_status):
        """服务重启后把任务对齐到明确终态。

        `Service.__init__` 会把在途 maintenance_jobs 标成 interrupted，但 pack_tasks
        不会自己跟着变——不同步的话，界面上会永远停在「处理中」。这里用真实的 job 状态
        对账：job 不存在（崩在派发之前）或已是终态而任务还没结束，就落到 failed
        并写明原因，不假装还在跑。
        """
        recovered = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,data FROM pack_tasks').fetchall()
            for row in rows:
                task = json.loads(row['data'])
                if task['status'] not in ('queued', 'running', 'cancel_requested', 'waiting_input'):
                    continue
                state = job_status(task.get('job_id'))
                if state in ('pending', 'running', 'cancel_requested'):
                    continue
                status = 'cancelled' if state == 'cancelled' else 'failed'
                task = {**task, 'status': status, 'error_code': 'interrupted',
                        'error': '服务重启时这次调用没有收到回执，已标为终态；请重新提交。',
                        'validation_status': 'unverified', 'updated_at': now()}
                db.execute('UPDATE pack_tasks SET data=? WHERE id=?', (_json(task), row['id']))
                db.execute('INSERT INTO pack_task_events(task_id,type,payload,at) VALUES(?,?,?,?)',
                           (row['id'], 'task.interrupted', _json({'job': state}), now()))
                recovered.append(row['id'])
        return recovered

    def request_cancel(self, task_id, *, actor):
        """取消是持久化的意图，不是只在内存里置个标志。"""
        task = self.task(task_id, actor=actor)
        if task['status'] in ('succeeded', 'failed', 'cancelled'):
            return task
        return self.update_task(task_id, {'status': 'cancel_requested'}, event=('task.cancel_requested', {}))

    def begin_evaluation(self, pack_id, *, content_digest_value, actor, operation_key, job_id):
        """在**提交时**就登记幂等键，而不是等验证跑完才登记。

        先前的写法只在 save_evaluation 里登记，于是同一个键重复提交会重复真实执行一遍
        测试集；丢响应的重试因此变成「跑两次、留两条证据」。
        """
        fingerprint = self._fingerprint({'kind': 'evaluation', 'pack': pack_id,
                                         'digest': content_digest_value})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pack = self._pack_row(db, pack_id)
            self._require_maintainer(pack, actor)
            replay = self._replay(db, actor['id'], operation_key, fingerprint)
            if replay is not None:
                return {**replay, 'idempotent_replay': True}
            record = {'job_id': job_id, 'pack_id': pack_id, 'content_digest': content_digest_value}
            return {**self._record(db, actor['id'], operation_key, fingerprint, record),
                    'idempotent_replay': False}

    def tasks_for(self, actor, *, limit=50):
        with self.store.connect() as db:
            rows = db.execute('SELECT data FROM pack_tasks ORDER BY rowid DESC LIMIT 500')
            tasks = [json.loads(r['data']) for r in rows]
        mine = [t for t in tasks if str(t['actor_id']) == str(actor['id']) or actor.get('role') == 'admin']
        return mine[:limit]
