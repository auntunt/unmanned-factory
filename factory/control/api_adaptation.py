"""Manual third-party API adaptation tasks: one contract, one field mapping
matrix, one real business message, one honest receipt.

This mirrors the shape of ``factory.control.issue_maintenance`` deliberately --
same explicit-port design, same append-only receipts, same "derive state from
the execution port, never store a second opinion of it" discipline -- but it is
its own module with its own source type, because an adaptation task is not a
maintenance task wearing a different label: what it persists (a contract
version, a field-by-field mapping matrix, a business message and its real
technical/business outcome) is different data with different honesty rules.

Deliberate non-goals, each of which would be a second source of truth:

* No status column. A task's status is derived from the execution port on
  every read, exactly as in ``issue_maintenance``.
* No silent field drop. Every source field in the mapping matrix is either
  mapped or explicitly marked unmapped with a stated impact; ``normalize``
  refuses a mapping entry that is neither.
* No self-reported business outcome. ``technical_success`` / ``business_accepted``
  / ``business_completed`` are read out of a check's own captured stdout after
  a real call was made from the delivered working copy, never invented from an
  HTTP status code and never taken on the coding executor's word.
* No second executor, queue or contract-version table. ``submit`` hands a
  package to whatever the adapter wired in (``svc.start_plan`` in production),
  and a new contract version is a new revision of the same task record, exactly
  the way ``issue_maintenance.revise`` reopens a task -- so the predecessor's
  mapping matrix and receipts are never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from factory.control.store import Conflict, now

SCHEMA_VERSION = 1

#: What a caller may see. Derived, never stored -- same discipline as
#: ``issue_maintenance.STATES``.
STATES = ('received', 'running', 'waiting', 'cancelling', 'delivered', 'failed',
          'cancelled')

#: Execution-side vocabulary mapped onto ours. Kept as this module's own copy
#: rather than imported from ``issue_maintenance``: the run-state vocabulary is
#: generic to the execution engine, not owned by the maintenance module, and an
#: unmapped upstream state must still make *this* module raise rather than
#: silently inherit whatever ``issue_maintenance`` happens to tolerate today.
EXECUTION_STATES = {
    'received': 'received', 'queued': 'running', 'planning': 'running',
    'running': 'running', 'verifying': 'running',
    'needs_human': 'waiting', 'awaiting_approval': 'waiting',
    'ready_for_review': 'delivered', 'published': 'delivered',
    'failed': 'failed', 'cancelled': 'cancelled', 'discarded': 'cancelled',
}

#: Where a delivery may land. Adaptation work ships as a patch by default; this
#: module does not deploy anything.
DELIVERY_TIERS = ('package', 'authorized_test_project')

#: Declared at contract-import time. ``mock`` is a local test double (what this
#: module's own vertical-slice test uses), ``sandbox`` is the vendor's own
#: non-production environment, ``production`` is the real third party. This
#: travels into the receipt so "the call succeeded" is never read as "against
#: the real vendor" unless it said so at intake.
ENVIRONMENTS = ('mock', 'sandbox', 'production')

_SHA = re.compile(r'^[0-9a-f]{40}$')
_KEY = re.compile(r'^[A-Za-z0-9_-]{8,100}$')
#: What a check prints on its own stdout after it made a real call. Read back
#: out of the captured process output, never out of anything the coding
#: executor asserts about itself.
_RESULT_LINE = re.compile(r'ADAPTATION_RESULT:\s*(\{.*\})')

_REQUIRED = ('project_id', 'repository', 'base_sha', 'contract', 'mapping_matrix',
             'message', 'expected_behaviour', 'delivery_goal', 'agreement',
             'idempotency_key')
_CONTRACT_REQUIRED = ('source_api', 'target_adapter')
_SOURCE_API_REQUIRED = ('name', 'version', 'base_url', 'auth', 'environment')
_TARGET_ADAPTER_REQUIRED = ('name', 'entry')
_MESSAGE_REQUIRED = ('name', 'method', 'path')


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def contract_digest(contract) -> str:
    """Identity of the contract *text*, so a same-name new version is visible."""
    return _digest(contract)


def _normalize_mapping_entry(entry) -> dict:
    """One row of the field mapping matrix.

    A field is either mapped (needs a target field) or explicitly not mapped
    (needs a stated impact). There is no third option that lets a field pass
    through unaccounted for -- that silent-drop path is exactly what SHARED.md
    and the pack's acceptance criteria forbid.
    """
    if not isinstance(entry, dict):
        raise ValueError('字段映射矩阵的每一项必须是一个对象')
    source_field = entry.get('source_field')
    if not source_field:
        raise ValueError('字段映射缺少 source_field')
    mapped = bool(entry.get('mapped', bool(entry.get('target_field'))))
    if mapped and not entry.get('target_field'):
        raise ValueError(f'字段 {source_field} 标记为已映射，但缺少 target_field')
    if not mapped and not entry.get('impact'):
        raise ValueError(f'字段 {source_field} 无法映射，必须说明 impact（对业务的影响），不能静默丢弃')
    return {
        'source_field': str(source_field),
        'mapped': mapped,
        'target_field': str(entry.get('target_field') or ''),
        'type': str(entry.get('type') or ''),
        'unit': str(entry.get('unit') or ''),
        'enum': [str(v) for v in (entry.get('enum') or [])],
        'null_semantics': str(entry.get('null_semantics') or ''),
        'precision': str(entry.get('precision') or ''),
        'encoding': str(entry.get('encoding') or ''),
        'timezone': str(entry.get('timezone') or ''),
        'error_code': str(entry.get('error_code') or ''),
        'transform': str(entry.get('transform') or ''),
        'impact': str(entry.get('impact') or ''),
        'notes': str(entry.get('notes') or ''),
    }


def normalize(request) -> dict:
    """Validate an adaptation request and return its canonical form.

    Everything rejected here is rejected before dispatch: a task with no
    resolvable baseline, no field accounted for, or a non-idempotent message
    with no stated retry/dedup policy must never reach a paid call.
    """
    if not isinstance(request, dict):
        raise ValueError('接口适配任务请求必须是一个对象')
    missing = [key for key in _REQUIRED if not request.get(key)]
    if missing:
        raise ValueError('接口适配任务缺少必填项：' + '、'.join(missing))

    contract = request['contract']
    if not isinstance(contract, dict):
        raise ValueError('contract 必须是一个对象')
    absent = [key for key in _CONTRACT_REQUIRED if not contract.get(key)]
    if absent:
        raise ValueError('contract 缺少：' + '、'.join(absent))
    source_api = contract['source_api']
    if not isinstance(source_api, dict):
        raise ValueError('contract.source_api 必须是一个对象')
    absent = [key for key in _SOURCE_API_REQUIRED if not source_api.get(key)]
    if absent:
        raise ValueError('contract.source_api 缺少：' + '、'.join(absent))
    if source_api['environment'] not in ENVIRONMENTS:
        raise ValueError('contract.source_api.environment 只能是：' + '、'.join(ENVIRONMENTS))
    target_adapter = contract['target_adapter']
    if not isinstance(target_adapter, dict):
        raise ValueError('contract.target_adapter 必须是一个对象')
    absent = [key for key in _TARGET_ADAPTER_REQUIRED if not target_adapter.get(key)]
    if absent:
        raise ValueError('contract.target_adapter 缺少：' + '、'.join(absent))
    fields_spec = contract.get('fields') or {}
    if not isinstance(fields_spec, dict):
        raise ValueError('contract.fields 必须是一个对象（字段名到规格的映射）')

    mapping_raw = request.get('mapping_matrix')
    if not isinstance(mapping_raw, list) or not mapping_raw:
        raise ValueError('字段映射矩阵不能为空，必须覆盖所有源字段')
    mapping_matrix = [_normalize_mapping_entry(entry) for entry in mapping_raw]
    seen = set()
    for entry in mapping_matrix:
        if entry['source_field'] in seen:
            raise ValueError(f"字段 {entry['source_field']} 在映射矩阵中重复")
        seen.add(entry['source_field'])

    message = request['message']
    if not isinstance(message, dict):
        raise ValueError('message 必须是一个对象')
    absent = [key for key in _MESSAGE_REQUIRED if not message.get(key)]
    if absent:
        raise ValueError('message 缺少：' + '、'.join(absent))
    idempotent = bool(message.get('idempotent', False))
    retry_policy = str(message.get('retry_policy') or '')
    if not idempotent and not retry_policy:
        raise ValueError('非幂等业务报文必须显式说明重试/去重策略，超时不许盲重试')

    if not _SHA.match(str(request['base_sha'])):
        raise ValueError('必须提供完整的 40 位基线 commit SHA；分支名只用于展示')
    if not _KEY.match(str(request['idempotency_key'])):
        raise ValueError('幂等键需为 8-100 位字母、数字、下划线或连字符')
    agreement = request['agreement']
    if not isinstance(agreement, dict) or not agreement.get('revision'):
        raise ValueError('必须记录适用的约定版本')
    tier = request.get('delivery_tier', 'package')
    if tier not in DELIVERY_TIERS:
        raise ValueError('交付层级只能是：' + '、'.join(DELIVERY_TIERS))

    normalized_contract = {
        'source_api': {key: str(source_api[key]) for key in _SOURCE_API_REQUIRED},
        'target_adapter': {key: str(target_adapter[key]) for key in _TARGET_ADAPTER_REQUIRED},
        'endpoints': [dict(e) for e in (contract.get('endpoints') or [])],
        'fields': {str(k): dict(v) for k, v in fields_spec.items()},
    }
    unmapped = [entry for entry in mapping_matrix if not entry['mapped']]
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': str(request['project_id']),
        'repository': str(request['repository']),
        'base_sha': str(request['base_sha']),
        'base_branch_label': str(request.get('base_branch_label') or ''),
        'contract': normalized_contract,
        'contract_digest': contract_digest(normalized_contract),
        'mapping_matrix': mapping_matrix,
        'unmapped_fields': unmapped,
        'message': {
            'name': str(message['name']), 'method': str(message['method']).upper(),
            'path': str(message['path']),
            'idempotent': idempotent, 'retry_policy': retry_policy,
            'sample_payload': dict(message.get('sample_payload') or {}),
        },
        'expected_behaviour': str(request['expected_behaviour']),
        'delivery_goal': str(request['delivery_goal']),
        'agreement': {'revision': str(agreement['revision']),
                      'skill_version': str(agreement.get('skill_version') or '')},
        'idempotency_key': str(request['idempotency_key']),
        'delivery_tier': tier,
        # Same reasoning as issue_maintenance.normalize: declared at import time
        # so the disclaimer comes from the submission, not from whoever writes
        # the receipt later.
        'synthetic': bool(request.get('synthetic')),
    }


def content_fingerprint(normalized) -> str:
    """Two imports are the same submission only if all of this is the same."""
    return _digest({key: normalized[key] for key in (
        'contract_digest', 'mapping_matrix', 'message', 'project_id', 'repository',
        'base_sha', 'expected_behaviour', 'delivery_goal', 'agreement',
        'delivery_tier', 'synthetic')})


def contract_diff(old_contract, new_contract) -> dict:
    """What changed between two contract versions, field by field.

    Compared on ``fields`` (the flat name -> spec map), not on the whole
    document: a change to ``endpoints`` description text is not a mapping
    concern, but a change to a field's type, precision or enum is exactly the
    thing a mapping matrix entry can silently go stale against.
    """
    old_fields = old_contract.get('fields', {}) or {}
    new_fields = new_contract.get('fields', {}) or {}
    added = sorted(set(new_fields) - set(old_fields))
    removed = sorted(set(old_fields) - set(new_fields))
    changed = sorted(name for name in (set(old_fields) & set(new_fields))
                      if old_fields[name] != new_fields[name])
    return {
        'source_api_version': {'old': old_contract['source_api']['version'],
                                'new': new_contract['source_api']['version']},
        'fields_added': added, 'fields_removed': removed, 'fields_changed': changed,
    }


def affected_mappings(diff, mapping_matrix) -> list[dict]:
    """Which mapping-matrix rows the new contract's changes actually touch.

    This is the "定位受影响映射" step: a caller importing a new spec version
    does not have to re-read every field by hand to find the ones a diff put in
    question.
    """
    touched_removed = set(diff['fields_removed'])
    touched_changed = set(diff['fields_changed'])
    out = []
    for entry in mapping_matrix:
        name = entry['source_field']
        if name in touched_removed:
            out.append({'source_field': name, 'target_field': entry['target_field'],
                        'reason': '字段已在新契约中移除，需要确认该映射是否失效'})
        elif name in touched_changed:
            out.append({'source_field': name, 'target_field': entry['target_field'],
                        'reason': '字段定义发生变化，需要重新核对该映射'})
    return out


class AdaptationStore:
    """Module-owned tables in the existing database. Same append-only receipts
    discipline as ``issue_maintenance.MaintenanceStore``."""

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS adaptation_tasks(
                id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS adaptation_task_keys(
                key TEXT PRIMARY KEY, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS adaptation_receipts(
                task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                data TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(task_id, revision));
            CREATE TRIGGER IF NOT EXISTS adaptation_receipts_no_update
                BEFORE UPDATE ON adaptation_receipts BEGIN
                SELECT RAISE(ABORT, 'adaptation receipts are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS adaptation_receipts_no_delete
                BEFORE DELETE ON adaptation_receipts BEGIN
                SELECT RAISE(ABORT, 'adaptation receipts are append-only');
            END;
            ''')

    def get(self, task_id) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM adaptation_tasks WHERE id=?',
                             (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row[0])

    def tasks(self, *, project_id=None) -> list[dict]:
        with self.store.connect() as db:
            records = [json.loads(row[0]) for row in
                       db.execute('SELECT data FROM adaptation_tasks')]
        if project_id is not None:
            records = [r for r in records if r['project_id'] == project_id]
        return sorted(records, key=lambda r: r['created_at'])

    def claim(self, key, normalized, fingerprint, *, predecessor=None) -> tuple[dict, bool]:
        """Reserve a task for this key, or return the one that already has it.

        Identical shape to ``MaintenanceStore.claim``: same key then same
        content decided inside one immediate transaction, so two concurrent
        imports of the same contract cannot both create a task.
        """
        task_id = uuid.uuid4().hex
        record = {'id': task_id,
                  'revision': (predecessor['revision'] + 1) if predecessor else 1,
                  'predecessor_id': predecessor['id'] if predecessor else None,
                  'successor_id': None, 'execution_id': None,
                  'cancel_requested': False, 'created_at': now(),
                  'content_fingerprint': fingerprint, **normalized}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT task_id,fingerprint FROM adaptation_task_keys WHERE key=?',
                             (key,)).fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint:
                    raise Conflict('这个适配任务已被接收；内容发生变化，请用新的幂等键重新提交。')
                existing = db.execute('SELECT data FROM adaptation_tasks WHERE id=?',
                                      (row['task_id'],)).fetchone()
                if existing is None:
                    raise Conflict('已登记的适配任务记录不可用，请刷新任务列表')
                return json.loads(existing[0]), False
            db.execute('INSERT INTO adaptation_task_keys VALUES (?,?,?)',
                       (key, task_id, fingerprint))
            db.execute('INSERT INTO adaptation_tasks VALUES (?,?)',
                       (task_id, json.dumps(record, ensure_ascii=False)))
            if predecessor is not None:
                prior = db.execute('SELECT data FROM adaptation_tasks WHERE id=?',
                                   (predecessor['id'],)).fetchone()
                if prior is None:
                    raise KeyError(predecessor['id'])
                old = json.loads(prior[0])
                if old.get('successor_id'):
                    raise Conflict('这个适配任务已经有一个后继修订，请在最新修订上继续')
                old['successor_id'] = task_id
                db.execute('UPDATE adaptation_tasks SET data=? WHERE id=?',
                           (json.dumps(old, ensure_ascii=False), predecessor['id']))
        return record, True

    def link(self, task_id, changes) -> dict:
        allowed = {'execution_id', 'successor_id', 'cancel_requested'}
        if set(changes) - allowed:
            raise ValueError('适配任务只允许补记执行引用、后继与取消意图')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM adaptation_tasks WHERE id=?',
                             (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            record = json.loads(row[0])
            if 'execution_id' in changes and record.get('execution_id') not in (
                    None, changes['execution_id']):
                raise Conflict('这个适配任务已经绑定了一次执行，不能改绑到另一次执行')
            record.update(changes)
            db.execute('UPDATE adaptation_tasks SET data=? WHERE id=?',
                       (json.dumps(record, ensure_ascii=False), task_id))
        return record

    def put_receipt(self, task_id, revision, receipt) -> dict:
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO adaptation_receipts VALUES (?,?,?,?)',
                       (task_id, revision, json.dumps(receipt, ensure_ascii=False), now()))
            row = db.execute('SELECT data FROM adaptation_receipts WHERE task_id=? AND revision=?',
                             (task_id, revision)).fetchone()
        return json.loads(row[0])

    def receipts(self, task_id) -> list[dict]:
        with self.store.connect() as db:
            return [{'revision': row['revision'], 'at': row['at'],
                     **json.loads(row['data'])}
                    for row in db.execute(
                        'SELECT revision,data,at FROM adaptation_receipts '
                        'WHERE task_id=? ORDER BY revision', (task_id,))]


def status_of(execution_state) -> str:
    if execution_state is None:
        return 'received'
    mapped = EXECUTION_STATES.get(execution_state)
    if mapped is None:
        raise Conflict(f'执行状态 {execution_state} 无法映射到适配任务状态，请人工核对')
    return mapped


class AdaptationTasks:
    """The Task port, shaped like ``issue_maintenance.MaintenanceTasks``.

    ``execution``, ``repository`` and ``identity`` are explicit ports. Nothing
    in this class knows about FastAPI, a request object, or a live SDK handle.

    Required port surface:

    * ``execution.submit(task, *, actor)`` -> execution id
      ``execution.status(execution_id)`` -> one of ``EXECUTION_STATES``
      ``execution.events(execution_id, after=0)`` -> ``[{sequence,kind,payload}]``
      ``execution.cost_usd(execution_id)`` -> float
      ``execution.cancel(execution_id, *, actor)``
      ``execution.delivery(execution_id)`` -> ``{commit, checks, unverified,
      working_copy_base_sha, repository, synthetic, business_call}`` or ``None``
      ``execution.export_artifacts(execution_id)`` -> ``{diff_hash, artifacts}``
    * ``repository.resolve(project_id, repository, base_sha)`` -> repository
      identity; raises ``ValueError`` when the SHA or repo does not match.
    * ``identity.require(actor, project_id)`` -> ``None``; raises on refusal.
    """

    def __init__(self, store, *, execution, repository, identity):
        self.records = AdaptationStore(store)
        self.execution = execution
        self.repository = repository
        self.identity = identity

    # -- creation ---------------------------------------------------------
    def create(self, request, *, actor) -> dict:
        normalized = normalize(request)
        self.identity.require(actor, normalized['project_id'])
        self.repository.resolve(normalized['project_id'], normalized['repository'],
                                normalized['base_sha'])
        key = f"adaptation:{normalized['project_id']}:{normalized['idempotency_key']}"
        record, created = self.records.claim(key, normalized,
                                            content_fingerprint(normalized))
        if created:
            # Only on the transaction that actually claimed the task: a retry
            # that lands on an already-claimed record must not write a second,
            # slightly different memory entry for the same contract.
            self._persist_contract_memory(record, actor=actor)
        self._bind_execution(record, actor=actor)
        return self.get(record['id'], actor=actor)

    def _persist_contract_memory(self, record, *, actor) -> None:
        """Contract version, source and field-mapping state, into project memory.

        Uses ``scenario_memory.record`` -- the single memory entry point
        B0-REUSE.md designates for all three scenarios -- rather than a
        parallel table. Written as ``candidate`` (the function's own default):
        nothing here has been confirmed by a human, it is only what this
        import observed. Unmapped fields get their own entry so "this field
        could not be mapped and here is the impact" survives even if nobody
        reads the full mapping matrix again before the next spec change.
        """
        from factory.control import scenario_memory
        store = self.records.store
        source_api = record['contract']['source_api']
        target = record['contract']['target_adapter']
        mapped = sum(1 for m in record['mapping_matrix'] if m['mapped'])
        topic = f"契约 {source_api['name']} v{source_api['version']} -> {target['name']}"
        content = (f"来源：{source_api['name']} v{source_api['version']}"
                  f"（{source_api['environment']}），base_url {source_api['base_url']}。"
                  f"目标 Adapter：{target['name']}（{target['entry']}）。"
                  f"字段映射：{mapped}/{len(record['mapping_matrix'])} 已映射。")
        try:
            scenario_memory.record(
                store, record['project_id'], plugin_id=PLUGIN_ID, topic=topic,
                content=content, actor=actor['username'], kind='fact',
                paths=[target['entry']], commit_sha=record['base_sha'])
        except (KeyError, ValueError):
            # A project with knowledge past its own size limit, or one whose
            # store cannot resolve, must not block registering the adaptation
            # task -- but this must not be confused with "nothing to record".
            pass
        if record['unmapped_fields']:
            impact = '；'.join(f"{m['source_field']}：{m['impact']}"
                              for m in record['unmapped_fields'])
            try:
                scenario_memory.record(
                    store, record['project_id'], plugin_id=PLUGIN_ID,
                    topic=f'{topic} 待确认字段', content=impact,
                    actor=actor['username'], kind='hypothesis',
                    paths=[target['entry']], commit_sha=record['base_sha'])
            except (KeyError, ValueError):
                pass

    def _bind_execution(self, record, *, actor) -> dict:
        """Give a claimed task its execution, whether or not this call claimed it.

        Same recoverable-binding reasoning as
        ``issue_maintenance_webuddy.WebuddyExecution`` / ``_bind_execution``:
        claiming the idempotency key and dispatching the work are two writes
        that cannot be made one, so a retry after an interrupted submit must be
        able to finish the binding rather than resubmit or hang forever.
        """
        if record.get('execution_id'):
            return record
        execution_id = self.execution.submit(record, actor=actor)
        return self.records.link(record['id'], {'execution_id': execution_id})

    def revise(self, task_id, request, *, actor) -> dict:
        """Import a next contract version: diff, locate affected mappings, and
        re-dispatch the same adaptation implementation as a new revision.

        The predecessor's mapping matrix and receipts stay exactly where they
        are -- this is a new revision that points back, never an overwrite, so
        the old version's evidence survives a spec change.
        """
        previous = self.records.get(task_id)
        self.identity.require(actor, previous['project_id'])
        normalized = normalize(request)
        if normalized['project_id'] != previous['project_id']:
            raise Conflict('新修订必须属于同一个项目')
        fingerprint = content_fingerprint(normalized)
        if fingerprint == previous['content_fingerprint']:
            raise Conflict('新修订的契约与映射跟原任务完全相同，无需重新导入')
        self.repository.resolve(normalized['project_id'], normalized['repository'],
                                normalized['base_sha'])
        diff = contract_diff(previous['contract'], normalized['contract'])
        normalized['contract_diff'] = diff
        normalized['affected_mappings'] = affected_mappings(diff, normalized['mapping_matrix'])
        key = f"adaptation:{normalized['project_id']}:{normalized['idempotency_key']}"
        record, created = self.records.claim(key, normalized, fingerprint,
                                            predecessor=previous)
        if created:
            self._persist_contract_memory(record, actor=actor)
            self._persist_diff_memory(record, actor=actor)
        self._bind_execution(record, actor=actor)
        return self.get(record['id'], actor=actor)

    def _persist_diff_memory(self, record, *, actor) -> None:
        """The contract diff itself, so "why did the mapping change" survives
        independently of anyone re-reading both contract versions side by side.
        """
        diff = record.get('contract_diff')
        if not diff:
            return
        from factory.control import scenario_memory
        content = (f"版本 {diff['source_api_version']['old']} -> "
                  f"{diff['source_api_version']['new']}；"
                  f"新增字段：{'、'.join(diff['fields_added']) or '无'}；"
                  f"移除字段：{'、'.join(diff['fields_removed']) or '无'}；"
                  f"变化字段：{'、'.join(diff['fields_changed']) or '无'}；"
                  f"受影响映射 {len(record.get('affected_mappings') or [])} 处。")
        try:
            scenario_memory.record(
                self.records.store, record['project_id'], plugin_id=PLUGIN_ID,
                topic=(f"契约变更 {diff['source_api_version']['old']}->"
                      f"{diff['source_api_version']['new']}"),
                content=content, actor=actor['username'], kind='decision',
                paths=[record['contract']['target_adapter']['entry']],
                commit_sha=record['base_sha'])
        except (KeyError, ValueError):
            pass

    # -- reading ----------------------------------------------------------
    def get(self, task_id, *, actor) -> dict:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        return self._view(record)

    def list(self, *, actor, project_id) -> list[dict]:
        self.identity.require(actor, project_id)
        return [self._view(record) for record in
                self.records.tasks(project_id=project_id)]

    def _view(self, record) -> dict:
        execution_id = record.get('execution_id')
        status = status_of(self.execution.status(execution_id) if execution_id else None)
        if status in ('running', 'waiting') and record.get('cancel_requested'):
            status = 'cancelling'
        delivery = self.execution.delivery(execution_id) if execution_id else None
        receipts = self.records.receipts(record['id'])
        mapping_matrix = record['mapping_matrix']
        return {
            'schema_version': SCHEMA_VERSION,
            'task_id': record['id'], 'revision': record['revision'],
            'predecessor_id': record.get('predecessor_id'),
            'successor_id': record.get('successor_id'),
            'status': status,
            'project_id': record['project_id'],
            'baseline': {'repository': record['repository'],
                         'base_sha': record['base_sha'],
                         'base_branch_label': record['base_branch_label']},
            'contract': record['contract'],
            'contract_digest': record['contract_digest'],
            'contract_diff': record.get('contract_diff'),
            'affected_mappings': record.get('affected_mappings') or [],
            'mapping_matrix': mapping_matrix,
            'unmapped_fields': record['unmapped_fields'],
            # HTTP operations, business messages and fields are counted
            # separately on purpose (SHARED.md): this task handles exactly one
            # business message against however many HTTP operations the
            # contract declares, over however many fields it maps.
            'counts': {'http_operations': len(record['contract'].get('endpoints') or []),
                      'business_messages': 1, 'fields': len(mapping_matrix)},
            'message': record['message'],
            'agreement': record['agreement'],
            'expected_behaviour': record['expected_behaviour'],
            'delivery_goal': record['delivery_goal'],
            'delivery_tier': record['delivery_tier'],
            'synthetic': record.get('synthetic', False),
            'execution_id': execution_id,
            'cost_usd': self.execution.cost_usd(execution_id) if execution_id else 0.0,
            'delivery': delivery,
            'business_call': (delivery or {}).get('business_call'),
            'receipts': receipts,
            'created_at': record['created_at'],
        }

    # -- lifecycle ----------------------------------------------------------
    def events(self, task_id, *, actor, after=0) -> list[dict]:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        if not record.get('execution_id'):
            return []
        return self.execution.events(record['execution_id'], after=after)

    def cancel(self, task_id, *, actor) -> dict:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        self.records.link(task_id, {'cancel_requested': True})
        if record.get('execution_id'):
            self.execution.cancel(record['execution_id'], actor=actor)
        return self.get(task_id, actor=actor)

    # -- delivery -------------------------------------------------------
    def export(self, task_id, *, actor) -> dict:
        """Freeze the receipt for a finished task: patch, mapping summary and
        the real business-call outcome, together, once.

        Called twice, this returns the first receipt, exactly like
        ``MaintenanceTasks.export`` -- the table refuses an update for this
        ``(task_id, revision)``.
        """
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        view = self._view(record)
        if view['status'] != 'delivered':
            raise Conflict(f"适配任务当前状态为 {view['status']}，还没有可交付的结果")
        delivery = view['delivery'] or {}
        if not delivery.get('commit'):
            raise Conflict('执行没有报告交付 commit，不能出回执')
        actual = delivery.get('working_copy_base_sha')
        if actual != record['base_sha']:
            raise Conflict('执行工作副本的基线与登记的基线不一致，拒绝出回执：'
                           f"登记 {record['base_sha']}，实际 {actual}")
        if delivery.get('repository') != record['repository']:
            raise Conflict('执行工作副本的仓库与登记的仓库不一致，拒绝出回执')
        exported = self.execution.export_artifacts(view['execution_id'])
        receipt = self.receipt(view, delivery, exported)
        stored = self.records.put_receipt(task_id, record['revision'], receipt)
        return {'receipt': stored, 'text': render_receipt(stored),
                'artifacts': exported['artifacts']}

    @staticmethod
    def receipt(view, delivery, exported) -> dict:
        """The machine-readable receipt. Unverified items and business outcome
        are part of it, not a footnote -- and the three business-call flags
        travel as three separate fields, never collapsed into one."""
        business_call = delivery.get('business_call')
        return {
            'schema': 'webuddy.adaptation.receipt/1',
            'task_id': view['task_id'], 'revision': view['revision'],
            'baseline': view['baseline'],
            'contract': {'source_api': view['contract']['source_api'],
                         'target_adapter': view['contract']['target_adapter']},
            'contract_digest': view['contract_digest'],
            'contract_diff': view.get('contract_diff'),
            'affected_mappings': view.get('affected_mappings') or [],
            'mapping_summary': {
                'fields': len(view['mapping_matrix']),
                'mapped': sum(1 for m in view['mapping_matrix'] if m['mapped']),
                'unmapped': len(view['unmapped_fields'])},
            'unmapped_fields': [{'source_field': m['source_field'], 'impact': m['impact']}
                                for m in view['unmapped_fields']],
            'message': view['message'],
            'counts': view['counts'],
            'delivery': {'commit': delivery.get('commit'),
                         'diff_hash': exported['diff_hash'],
                         'artifacts': [a['name'] for a in exported['artifacts']]},
            'agreement': view['agreement'],
            'checks': [{'name': c.get('name'), 'passed': c.get('passed'),
                        'exit_code': c.get('exit_code')}
                       for c in (delivery.get('checks') or [])],
            'unverified': list(delivery.get('unverified') or []),
            'business_call': ({
                'correlation_id': business_call.get('correlation_id'),
                'technical_success': business_call.get('technical_success'),
                'business_accepted': business_call.get('business_accepted'),
                'business_completed': business_call.get('business_completed'),
                'mock': bool(business_call.get('mock')),
                'endpoint': business_call.get('endpoint'),
                'sanitized_response': business_call.get('sanitized_response'),
            } if business_call else None),
            'delivery_tier': view['delivery_tier'],
            'synthetic': bool(view.get('synthetic') or delivery.get('synthetic')),
        }


def render_receipt(receipt) -> str:
    """The human-readable half of the same receipt, from the same data."""
    lines = [
        f"适配回执 {receipt['task_id']} 修订 {receipt['revision']}",
        f"源接口：{receipt['contract']['source_api']['name']} "
        f"v{receipt['contract']['source_api']['version']}",
        f"目标 Adapter：{receipt['contract']['target_adapter']['name']}"
        f"（{receipt['contract']['target_adapter']['entry']}）",
        f"基线：{receipt['baseline']['repository']} @ {receipt['baseline']['base_sha']}"
        + (f"（分支标注 {receipt['baseline']['base_branch_label']}）"
           if receipt['baseline']['base_branch_label'] else ''),
        f"业务报文：{receipt['message']['method']} {receipt['message']['path']}"
        f"（{receipt['message']['name']}）",
        f"统计：HTTP 操作 {receipt['counts']['http_operations']} 个，"
        f"业务报文 {receipt['counts']['business_messages']} 条，"
        f"字段 {receipt['counts']['fields']} 个",
        f"字段映射：{receipt['mapping_summary']['mapped']}/{receipt['mapping_summary']['fields']} "
        f"已映射，{receipt['mapping_summary']['unmapped']} 个待确认",
    ]
    if receipt['unmapped_fields']:
        lines.append('待确认字段（未静默丢弃）：')
        for item in receipt['unmapped_fields']:
            lines.append(f"  - {item['source_field']}：{item['impact']}")
    if receipt.get('contract_diff'):
        diff = receipt['contract_diff']
        lines.append(f"契约版本变化：{diff['source_api_version']['old']} -> "
                     f"{diff['source_api_version']['new']}")
        lines.append(f"  新增字段：{'、'.join(diff['fields_added']) or '（无）'}")
        lines.append(f"  移除字段：{'、'.join(diff['fields_removed']) or '（无）'}")
        lines.append(f"  变化字段：{'、'.join(diff['fields_changed']) or '（无）'}")
    if receipt.get('affected_mappings'):
        lines.append('受影响映射：')
        for item in receipt['affected_mappings']:
            lines.append(f"  - {item['source_field']}：{item['reason']}")
    lines.append(f"交付：commit {receipt['delivery']['commit']}"
                 f"，diff {receipt['delivery']['diff_hash']}")
    lines.append(f"  构建物：{'、'.join(receipt['delivery']['artifacts']) or '（无）'}")
    lines.append(f"适用约定版本：{receipt['agreement']['revision']}"
                 + (f"（Skill {receipt['agreement']['skill_version']}）"
                    if receipt['agreement'].get('skill_version') else ''))
    lines.append('检查结果：')
    for check in receipt['checks'] or [{'name': '（无）', 'passed': None, 'exit_code': None}]:
        verdict = '通过' if check['passed'] else '未通过' if check['passed'] is False else '未运行'
        lines.append(f"  - {check['name']}：{verdict}（退出码 {check['exit_code']}）")
    lines.append('业务调用结果（技术成功、业务受理、业务完成三项分开，互不替代）：')
    call = receipt.get('business_call')
    if call is None:
        lines.append('  - 未联调，没有可引用的真实业务报文结果')
    else:
        lines.append(f"  - 请求关联 ID：{call.get('correlation_id')}")
        lines.append(f"  - 端点：{call.get('endpoint')}")
        lines.append(f"  - 技术成功：{call.get('technical_success')}")
        lines.append(f"  - 业务受理：{call.get('business_accepted')}")
        lines.append(f"  - 业务最终完成：{call.get('business_completed')}"
                     + ('（本期为单向上报，无回执渠道确认最终完成，不等同于业务受理或技术成功）'
                        if call.get('business_completed') is False else ''))
        lines.append('  - 这是对本地模拟第三方端点的联调'
                     if call.get('mock') else '  - 这是对真实第三方环境的联调')
        lines.append(f"  - 脱敏响应：{json.dumps(call.get('sanitized_response') or {}, ensure_ascii=False)}")
    lines.append('未验证项：')
    for item in receipt['unverified'] or ['（无显式未验证项）']:
        lines.append(f'  - {item}')
    tier = {'package': '交包待发布，本次未部署到任何环境',
            'authorized_test_project': '已部署到明确授权的公司测试项目'}
    lines.append('部署层级：' + tier.get(receipt['delivery_tier'], receipt['delivery_tier']))
    if receipt['synthetic']:
        lines.append('本次交付基于合成示例契约，不代表任何客户验收案例。')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Wiring onto the existing webuddy control plane.
#
# Everything below delegates: runs live in ``Store``, dispatch goes through the
# existing service (``svc.start_plan``), and the working copy is the one
# ``execute_plan`` created -- the same production path
# ``issue_maintenance_webuddy.WebuddyExecution`` uses, reached through this
# module's own source type rather than through that module's class, so an
# adaptation execution can never be misread as a maintenance one or vice versa.
# ---------------------------------------------------------------------------

import subprocess
from pathlib import Path

#: Only this source type belongs to the adaptation module -- checked on the way
#: out as well as in, exactly like ``issue_maintenance_webuddy.SOURCE_TYPE``.
SOURCE_TYPE = 'api_adaptation'


def adaptation_prompt(record, memory: str = '') -> str:
    """The instruction the executor receives for one contract-import task.

    ``memory`` is the project's long-term constraints, already split by
    ``scenario_memory.constraints_block``, placed before the contract for the
    same reason ``issue_maintenance_webuddy.maintenance_prompt`` places it
    before the issue body: the executor reads the project's own confirmed
    requirements first.
    """
    contract = record['contract']
    source_api = contract['source_api']
    target = contract['target_adapter']
    message = record['message']
    unmapped = record['unmapped_fields']
    lines = [
        '按已授权的三方接口适配流程处理下面这条契约导入/变更任务。',
        f"仓库：{record['repository']}",
        f"必须基于基线 commit：{record['base_sha']}",
        f"适用约定版本：{record['agreement']['revision']}",
        f"目标 Adapter：{target['name']}（入口 {target['entry']}）",
        f"源接口：{source_api['name']} v{source_api['version']}，"
        f"环境：{source_api['environment']}，base_url：{source_api['base_url']}",
        f"鉴权：{source_api['auth']}（凭据必须从配置读取，绝不能硬编码到代码里）",
        f"本次处理的业务报文：{message['method']} {message['path']}（{message['name']}）",
    ]
    if message['idempotent']:
        lines.append('该报文是幂等操作。')
    else:
        lines.append(f"该报文不是幂等操作；重试/去重策略：{message['retry_policy']}。"
                     '超时不得盲重试——先核对宿主/网关是否已有重试与去重能力，避免两层重复执行。')
    lines += [
        f"期望行为：{record['expected_behaviour']}",
        f"交付目标：{record['delivery_goal']}",
    ]
    if unmapped:
        lines.append('以下源字段无法映射，已标为待确认，实现中不得静默丢弃：')
        for entry in unmapped:
            lines.append(f"  - {entry['source_field']}：{entry['impact']}")
    if record.get('contract_diff'):
        diff = record['contract_diff']
        lines.append(f"这是一次契约变更导入（{diff['source_api_version']['old']} -> "
                     f"{diff['source_api_version']['new']}），需要更新同一适配实现，"
                     '不要重写整个 Adapter，也不要丢弃旧版本的证据。')
        for item in record.get('affected_mappings') or []:
            lines.append(f"  - 受影响映射 {item['source_field']}：{item['reason']}")
    if memory:
        lines += ['', memory]
    lines += [
        '',
        '字段映射矩阵（字段名/类型/单位/枚举/空值语义/精度/编码/时区/错误码/转换说明）：',
        json.dumps(record['mapping_matrix'], ensure_ascii=False, indent=2),
        '',
        '实现要求，逐条满足：',
        '1. 按映射矩阵实现/修改适配代码：处理有证据的字段转换、必填校验、枚举值、精度与时区差异；'
        '日志中的敏感字段（凭据、令牌、完整报文等）必须脱敏。',
        '2. 凭据从配置读取，不硬编码。',
        '3. 用这一条真实的业务报文，实际调用配置的 base_url（可能指向本地模拟的第三方端点），'
        '正确解释响应：技术是否成功（如 HTTP 状态码）、业务是否受理、业务是否最终完成——'
        '这三者必须分开判断，不能互相替代。HTTP 200 或对本地模拟端调用成功都不等于业务最终完成。',
        '4. 项目已配置的检查必须在完成这条业务报文的真实调用后，向标准输出打印恰好一行：\n'
        '   ADAPTATION_RESULT: {"correlation_id": "...", "technical_success": true/false, '
        '"business_accepted": true/false, "business_completed": true/false, '
        '"mock": true/false, "endpoint": "...", "sanitized_response": {...}}\n'
        '   sanitized_response 中的鉴权凭据、令牌等敏感字段必须脱敏或省略，不得原样打印。',
        '5. 先确认项目已配置的检查真的执行了这一条业务报文（而不是只做了格式校验），再产出结果。'
        '不要修改测试或检查基础设施本身。',
    ]
    return '\n'.join(lines)


class AdaptationIdentity:
    """Server-side identity. An actor arrives already resolved, never as text.

    Same shape as ``issue_maintenance_webuddy.WebuddyIdentity``, kept as this
    module's own class rather than an import: an adaptation surface must not
    silently start trusting whatever refusal rules a future maintenance-only
    change adds to that class.
    """

    def __init__(self, governance=None):
        self.governance = governance

    def require(self, actor, project_id) -> None:
        if not isinstance(actor, dict) or not actor.get('id') or not actor.get('username'):
            raise Conflict('接口适配任务需要一个已解析的受控身份，请求体中的角色字段不构成授权')
        if self.governance is not None:
            self.governance.require_project(actor['id'], project_id)


class AdaptationRepository:
    """Resolve a pinned baseline against the real checkout, before any dispatch."""

    def __init__(self, store, *, timeout_s=15):
        self.store = store
        self.timeout_s = timeout_s

    def resolve(self, project_id, repository, base_sha) -> dict:
        project = self.store.project(project_id)
        if project.get('repository') != repository:
            raise ValueError(f"项目登记的仓库是 {project.get('repository')}，与请求的 {repository} 不一致")
        workspace = Path(project['workspace']).expanduser()
        if not workspace.is_dir():
            raise ValueError('项目工作区不存在，无法核对基线')
        done = subprocess.run(['git', 'cat-file', '-e', f'{base_sha}^{{commit}}'],
                              cwd=workspace, capture_output=True, timeout=self.timeout_s)
        if done.returncode != 0:
            raise ValueError(f'基线 commit {base_sha} 在这个仓库里不存在，请确认后重新提交')
        return {'project_id': project_id, 'repository': repository,
                'workspace': str(workspace), 'base_sha': base_sha}


def _unwired(name):
    def refuse(execution_id, actor):
        raise Conflict(f'本进程没有接入{name}，无法对执行 {execution_id} 执行该操作')
    return refuse


class AdaptationExecution:
    """The Execution port over the existing run store and dispatcher.

    Same dedup and recoverable-submit reasoning as
    ``issue_maintenance_webuddy.WebuddyExecution``, with its own source type
    and its own reading of a real business-call outcome out of a check's
    captured stdout (``_business_call``), which that class has no notion of.
    """

    def __init__(self, store, *, dispatch, cost, cancel=None, timeout_s=15):
        self.store = store
        self.dispatch = dispatch
        self.cost = cost
        self.cancel_hook = cancel or _unwired('cancel')
        self.timeout_s = timeout_s

    def submit(self, record, *, actor) -> str:
        from factory.control import scenario_memory
        try:
            memory = scenario_memory.constraints_block(
                scenario_memory.recall(self.store, record['project_id']))
        except (KeyError, ValueError):
            memory = '（项目记忆暂时读不到，本次没有携带已确认约束）'
        run, created = self.store.create_run(
            record['project_id'], adaptation_prompt(record, memory),
            source={'type': SOURCE_TYPE, 'actor': actor['username'],
                    'actor_id': actor['id'],
                    'adaptation_task_id': record['id'],
                    'adaptation_revision': record['revision'],
                    'contract_digest': record['contract_digest'],
                    'agreement_revision': record['agreement']['revision'],
                    'request_fingerprint': record['content_fingerprint'],
                    'repository': record['repository'],
                    'expected_base_sha': record['base_sha'],
                    'delivery_tier': record['delivery_tier'],
                    'synthetic': record.get('synthetic', False)},
            delivery_id=f"adaptation:{record['id']}",
            semantic_id=f"adaptation:{record['project_id']}:{record['content_fingerprint']}")
        if created or not self._dispatched(run):
            self.dispatch(run['id'])
        return run['id']

    def _dispatched(self, run) -> bool:
        """Has this run ever been handed to the durable queue? Same reasoning
        as ``WebuddyExecution._dispatched``: ``create_run``'s dedup answers
        "does an execution exist", not "was it ever dispatched"."""
        if run.get('status') != 'received':
            return True
        from factory.control.autonomy import DurableQueue
        return bool(DurableQueue(self.store).jobs(run['id']))

    def _run(self, execution_id) -> dict:
        run = self.store.get(execution_id)
        if (run.get('source') or {}).get('type') != SOURCE_TYPE:
            raise Conflict('这个执行不属于接口适配模块，拒绝当作适配证据读取')
        return run

    def status(self, execution_id) -> str:
        return self._run(execution_id)['status']

    def cost_usd(self, execution_id) -> float:
        return float(self.cost(execution_id))

    def events(self, execution_id, after=0) -> list[dict]:
        self._run(execution_id)
        return [{'sequence': event['id'], 'kind': event['type'],
                 'payload': event['payload'], 'at': event['at']}
                for event in self.store.events(execution_id, after=after)]

    def cancel(self, execution_id, *, actor):
        return self.cancel_hook(execution_id, actor)

    @staticmethod
    def _business_call(checks) -> dict | None:
        """Parse the ``ADAPTATION_RESULT`` line a check printed, if any.

        Read out of the check's own captured stdout -- produced by code that
        genuinely ran the request against whatever ``base_url`` the contract
        named -- never out of anything the coding provider claims about
        itself. The first check that printed a well-formed line wins; a check
        that printed nothing parseable contributes nothing, which is why a
        task can be technically ``delivered`` (patch, checks passing) with
        ``business_call`` still ``None`` -- delivered is not the same claim as
        联调过.
        """
        for check in checks:
            text = str(check.get('stdout') or '')
            match = _RESULT_LINE.search(text)
            if not match:
                continue
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            return {
                'correlation_id': payload.get('correlation_id'),
                'technical_success': payload.get('technical_success'),
                'business_accepted': payload.get('business_accepted'),
                'business_completed': payload.get('business_completed'),
                'mock': bool(payload.get('mock')),
                'endpoint': payload.get('endpoint'),
                'sanitized_response': payload.get('sanitized_response'),
                'check_name': check.get('name'),
            }
        return None

    def delivery(self, execution_id) -> dict | None:
        run = self._run(execution_id)
        artifacts = run.get('artifacts') or {}
        if not artifacts.get('commit'):
            return None
        source = run.get('source') or {}
        checks = artifacts.get('checks') or []
        return {
            'commit': artifacts['commit'],
            'checks': [{'name': check.get('name'), 'passed': check.get('exit') == 0,
                        'exit_code': check.get('exit'),
                        'reused': bool(check.get('reused')),
                        'identity_fingerprint': check.get('identity_fingerprint')}
                       for check in checks],
            'unverified': list(artifacts.get('unverified') or []),
            'working_copy_base_sha': self._working_copy_base_sha(artifacts),
            'repository': source.get('repository'),
            'synthetic': bool(source.get('synthetic')),
            'worktree': artifacts.get('worktree'),
            'business_call': self._business_call(checks),
        }

    def _working_copy_base_sha(self, artifacts) -> str | None:
        """The recorded baseline, but only if the delivered commit descends
        from it. Same cross-check as ``WebuddyExecution._working_copy_base_sha``."""
        worktree, commit = artifacts.get('worktree'), artifacts.get('commit')
        base = artifacts.get('base_sha')
        if not worktree or not commit or not base or not Path(worktree).is_dir():
            return None
        done = subprocess.run(
            ['git', 'merge-base', '--is-ancestor', base, commit], cwd=worktree,
            capture_output=True, timeout=self.timeout_s)
        return base if done.returncode == 0 else None

    def export_artifacts(self, execution_id) -> dict:
        """Export the patch from the working copy and hash the exported bytes."""
        run = self._run(execution_id)
        artifacts = run.get('artifacts') or {}
        worktree, commit = artifacts.get('worktree'), artifacts.get('commit')
        base = self._working_copy_base_sha(artifacts)
        if not base:
            raise Conflict('执行工作副本无法确认基线，不能导出补丁')
        done = subprocess.run(['git', 'format-patch', '--stdout', f'{base}..{commit}'],
                              cwd=worktree, capture_output=True, timeout=self.timeout_s)
        if done.returncode != 0 or not done.stdout:
            raise Conflict('执行工作副本没有可导出的补丁')
        return {'diff_hash': hashlib.sha256(done.stdout).hexdigest(),
                'artifacts': [{'name': f'adaptation-{execution_id}.patch',
                               'bytes': done.stdout}]}


#: The plugin this module's business surface belongs to.
PLUGIN_ID = 'api-adaptation'

#: Which availability class each business-port method falls into, mirroring
#: ``plugins.MAINTENANCE_ACTIONS``. A method not listed here is not reachable
#: through the gated port at all. There is currently no ``continue``-classified
#: method: this module has no human-in-the-loop resume/intervene surface yet,
#: and adding one later means classifying it here deliberately rather than it
#: silently falling through as ``always``.
ACTIONS = {
    'create': 'create',
    'revise': 'create',
    'get': 'always',
    'list': 'always',
    'events': 'always',
    'cancel': 'always',
    'export': 'always',
}

#: Task states that mean work is still live, as the existing engine counts it.
ACTIVE_TASK_STATES = frozenset({'received', 'running', 'waiting', 'cancelling'})


def active_task_count(store) -> int:
    """How many adaptation tasks are still live, across every project.

    Same shape as ``issue_maintenance_webuddy.active_task_count``: read from
    the same records and run states the task view derives from, and an
    unmappable or missing execution counts as live -- the safe direction for a
    question that decides whether a plugin stop is allowed.
    """
    live = 0
    for record in AdaptationStore(store).tasks():
        execution_id = record.get('execution_id')
        if not execution_id:
            live += 1
            continue
        try:
            run = store.get(execution_id)
        except KeyError:
            live += 1
            continue
        try:
            status = status_of(run.get('status'))
        except Conflict:
            live += 1
            continue
        if status in ('running', 'waiting') and record.get('cancel_requested'):
            status = 'cancelling'
        live += status in ACTIVE_TASK_STATES
    return live


def availability_for(store):
    """The availability store over this control database. Same table the
    integrator's ``plugins.PluginAvailability`` already owns; this module does
    not construct its own state.
    """
    from factory.control.plugins import PluginAvailability
    return PluginAvailability(store)


def tasks_for(svc, *, identity=None, dispatch=None, availability=None):
    """Assemble the Task port from a live service. One wiring, web and CLI alike.

    The return value is always gated through ``plugins.gated`` with this
    module's own ``ACTIONS`` -- there is no ungated form to reach. Until an
    integrator flips ``plugins.DECLARATIONS['api-adaptation'].executable`` to
    ``True`` (out of this module's scope: ``OWNERSHIP.md`` reserves
    ``plugins.py`` for the integrator), the plugin's default availability state
    is ``disabled`` and every ``create``/``revise`` call is refused by the gate
    -- the port itself is real and independently testable, but it is not yet
    reachable through the gated assembly in production.
    """
    from factory.control.plugins import gated
    store = svc.store
    port = AdaptationTasks(
        store,
        execution=AdaptationExecution(
            store, dispatch=svc.start_plan if dispatch is None else dispatch,
            cost=lambda eid: svc._usage(eid)['known_cost_usd']),
        repository=AdaptationRepository(store),
        identity=AdaptationIdentity(svc.governance) if identity is None else identity)
    return gated(port,
                 availability if availability is not None else availability_for(store),
                 PLUGIN_ID, ACTIONS)
