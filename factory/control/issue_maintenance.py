"""Manual issue maintenance tasks: one identity, one pinned baseline, one receipt.

This module owns only what no existing table owns: which issue (and which
version of it) a maintenance task came from, which repository and which full
commit it was pinned to, which agreement version judged it, which execution
carried it out, and what the delivery receipt said.  Everything else --
permissions, money, the execution state machine, the event log -- stays in the
control layer and is reached through the explicit ports below.

Deliberate non-goals, because each one would create a second source of truth:

* No status column.  A task's status is derived from the execution port on every
  read.  The only thing persisted about progress is a cancel *request*, which is
  an intent, not a state.
* No SQL against ``runs``/``events``.  The execution reference is an opaque id.
* No second executor, budget or queue.  ``submit`` hands a package to whatever
  the adapter wired in.
* No frontend session import.  An actor reaches this module already resolved by
  the identity port.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from factory.control.store import Conflict, now

SCHEMA_VERSION = 1

#: What a caller may see. Derived, never stored. ``cancelling`` is the one state
#: with no execution-side counterpart: it means a cancel was requested and the
#: execution has not yet reached a terminal state, which is a fact about our own
#: record, not a second opinion about the execution's.
STATES = ('received', 'running', 'waiting', 'cancelling', 'delivered', 'failed',
          'cancelled')

#: Execution-side vocabulary mapped onto ours. An execution state that is not
#: listed here is not silently treated as "probably running": ``status_of``
#: raises, because a maintenance surface that invents a state for an unknown
#: upstream value is exactly the second unexplained state source we refused.
EXECUTION_STATES = {
    'received': 'received', 'queued': 'running', 'planning': 'running',
    'running': 'running', 'verifying': 'running',
    'needs_human': 'waiting', 'awaiting_approval': 'waiting',
    'ready_for_review': 'delivered', 'published': 'delivered',
    'failed': 'failed', 'cancelled': 'cancelled', 'discarded': 'cancelled',
}

#: Where a delivery may land. ``package`` is the default for customer work: the
#: patch/artifact is exported and handed over, nothing is published anywhere.
DELIVERY_TIERS = ('package', 'authorized_test_project')

_SHA = re.compile(r'^[0-9a-f]{40}$')
_KEY = re.compile(r'^[A-Za-z0-9_-]{8,100}$')

_REQUIRED = ('issue', 'project_id', 'repository', 'base_sha',
             'expected_behaviour', 'delivery_goal', 'agreement',
             'idempotency_key')
_ISSUE_REQUIRED = ('source', 'external_id', 'version', 'title', 'body')


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def issue_digest(issue) -> str:
    """Identity of the issue *text*, so a reopen with new wording is visible."""
    return _digest({key: issue[key] for key in _ISSUE_REQUIRED})


def normalize(request) -> dict:
    """Validate a maintenance request and return its canonical form.

    Everything rejected here is rejected *before* dispatch, which is the point:
    a task with no resolvable baseline must never reach a paid call.
    """
    if not isinstance(request, dict):
        raise ValueError('维护任务请求必须是一个对象')
    missing = [key for key in _REQUIRED if not request.get(key)]
    if missing:
        raise ValueError('维护任务缺少必填项：' + '、'.join(missing))
    issue = request['issue']
    if not isinstance(issue, dict):
        raise ValueError('Issue 必须是一个对象')
    absent = [key for key in _ISSUE_REQUIRED if not issue.get(key)]
    if absent:
        raise ValueError('Issue 缺少原文或版本信息：' + '、'.join(absent))
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
    return {
        'schema_version': SCHEMA_VERSION,
        'issue': {key: str(issue[key]) for key in _ISSUE_REQUIRED},
        'issue_digest': issue_digest(issue),
        'project_id': str(request['project_id']),
        'repository': str(request['repository']),
        'base_sha': str(request['base_sha']),
        # Display only. It is recorded so a receipt can say what a human called
        # the baseline, and it is never used to resolve a checkout.
        'base_branch_label': str(request.get('base_branch_label') or ''),
        'expected_behaviour': str(request['expected_behaviour']),
        'delivery_goal': str(request['delivery_goal']),
        'agreement': {'revision': str(agreement['revision']),
                      'skill_version': str(agreement.get('skill_version') or '')},
        'idempotency_key': str(request['idempotency_key']),
        'delivery_tier': tier,
        # Declared at import time and carried into the execution source, so the
        # receipt's "this is a synthetic example" label comes from the submission
        # rather than from whoever later writes the receipt. Default is False: a
        # real delivery must not be able to acquire the disclaimer by omission,
        # and a demo must state that it is one.
        'synthetic': bool(request.get('synthetic')),
    }


def content_fingerprint(normalized) -> str:
    """Two imports are the same submission only if all of this is the same."""
    return _digest({key: normalized[key] for key in (
        'issue_digest', 'project_id', 'repository', 'base_sha',
        'expected_behaviour', 'delivery_goal', 'agreement', 'delivery_tier',
        'synthetic')})


_DIFF_GIT_HEADER = re.compile(r'^diff --git a/(?P<a>.+) b/(?P<b>.+)$', re.MULTILINE)


def _looks_like_a_safe_relpath(path) -> bool:
    """A conservative filter, not the authority on path safety.

    ``KnowledgeStore`` already enforces the real rule when the path is written;
    this only keeps an odd diff header (a rename through a path with a stray
    character, a submodule gitlink line) from turning an auxiliary "what files
    changed" note into a reason the whole export fails.
    """
    if not path or path.startswith('/') or '\\' in path or '\x00' in path:
        return False
    parts = path.split('/')
    return all(part not in ('', '.', '..') for part in parts)


def paths_from_patch(artifacts) -> list[str]:
    """The files a delivered patch actually touches, read from its own diff headers.

    Derived from the exported bytes -- the same bytes the receipt's ``diff_hash``
    covers -- rather than asked of the execution port as a second claim that
    could disagree with what was actually handed over. Order is the order the
    headers appear in; at most 20, matching project memory's own cap on paths
    per entry.
    """
    paths, seen = [], set()
    for artifact in artifacts or ():
        content = artifact.get('bytes') if isinstance(artifact, dict) else None
        if not isinstance(content, (bytes, bytearray)):
            continue
        text = bytes(content).decode('utf-8', errors='replace')
        for match in _DIFF_GIT_HEADER.finditer(text):
            path = match.group('b')
            if path and path not in seen and _looks_like_a_safe_relpath(path):
                seen.add(path)
                paths.append(path)
    return paths[:20]


#: The scenario prefix ``scenario_memory.title_for`` adds is a few characters;
#: staying well clear of its 200-character field cap leaves room for it without
#: this module having to import that constant to compute the exact remainder.
_MEMORY_TOPIC_MAX = 180
_MEMORY_CONTENT_MAX = 4000


def _bounded(text, limit) -> str:
    """Truncate with a visible marker rather than let a downstream cap reject it."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '…（已截断）'


class MaintenanceStore:
    """Module-owned tables in the existing database.

    The append-only trigger on ``maintenance_receipts`` is what makes "a new
    revision does not overwrite old evidence" a property of the database rather
    than a property of the code path a caller happened to take.
    """

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS maintenance_tasks(
                id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_task_keys(
                key TEXT PRIMARY KEY, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS maintenance_receipts(
                task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                data TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(task_id, revision));
            CREATE TRIGGER IF NOT EXISTS maintenance_receipts_no_update
                BEFORE UPDATE ON maintenance_receipts BEGIN
                SELECT RAISE(ABORT, 'maintenance receipts are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS maintenance_receipts_no_delete
                BEFORE DELETE ON maintenance_receipts BEGIN
                SELECT RAISE(ABORT, 'maintenance receipts are append-only');
            END;
            ''')

    def get(self, task_id) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM maintenance_tasks WHERE id=?',
                             (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row[0])

    def tasks(self, *, project_id=None) -> list[dict]:
        with self.store.connect() as db:
            records = [json.loads(row[0]) for row in
                       db.execute('SELECT data FROM maintenance_tasks')]
        if project_id is not None:
            records = [r for r in records if r['project_id'] == project_id]
        return sorted(records, key=lambda r: r['created_at'])

    def claim(self, key, normalized, fingerprint, *, predecessor=None) -> tuple[dict, bool]:
        """Reserve a task for this key, or return the one that already has it.

        The whole decision -- same key, then same content -- happens inside one
        immediate transaction, so two concurrent imports cannot both create a
        task and cannot both open a workspace.
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
            row = db.execute('SELECT task_id,fingerprint FROM maintenance_task_keys WHERE key=?',
                             (key,)).fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint:
                    raise Conflict('这个维护任务已被接收；内容发生变化，请用新的幂等键重新提交。')
                existing = db.execute('SELECT data FROM maintenance_tasks WHERE id=?',
                                      (row['task_id'],)).fetchone()
                if existing is None:
                    raise Conflict('已登记的维护任务记录不可用，请刷新任务列表')
                return json.loads(existing[0]), False
            db.execute('INSERT INTO maintenance_task_keys VALUES (?,?,?)',
                       (key, task_id, fingerprint))
            db.execute('INSERT INTO maintenance_tasks VALUES (?,?)',
                       (task_id, json.dumps(record, ensure_ascii=False)))
            if predecessor is not None:
                # Same transaction as the successor's own row: a crash between
                # the two would otherwise leave a revision nobody can reach from
                # the task a human is looking at.
                prior = db.execute('SELECT data FROM maintenance_tasks WHERE id=?',
                                   (predecessor['id'],)).fetchone()
                if prior is None:
                    raise KeyError(predecessor['id'])
                old = json.loads(prior[0])
                if old.get('successor_id'):
                    raise Conflict('这个维护任务已经有一个后继修订，请在最新修订上继续')
                old['successor_id'] = task_id
                db.execute('UPDATE maintenance_tasks SET data=? WHERE id=?',
                           (json.dumps(old, ensure_ascii=False), predecessor['id']))
        return record, True

    def link(self, task_id, changes) -> dict:
        """Attach a derived reference (execution id, successor, cancel intent).

        Only these keys: the submitted content of a task is immutable, and an
        update path that could rewrite ``base_sha`` would make the receipt's
        baseline unfalsifiable.
        """
        allowed = {'execution_id', 'successor_id', 'cancel_requested'}
        if set(changes) - allowed:
            raise ValueError('维护任务只允许补记执行引用、后继与取消意图')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM maintenance_tasks WHERE id=?',
                             (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            record = json.loads(row[0])
            if 'execution_id' in changes and record.get('execution_id') not in (
                    None, changes['execution_id']):
                raise Conflict('这个维护任务已经绑定了一次执行，不能改绑到另一次执行')
            record.update(changes)
            db.execute('UPDATE maintenance_tasks SET data=? WHERE id=?',
                       (json.dumps(record, ensure_ascii=False), task_id))
        return record

    def has_receipt(self, task_id, revision) -> bool:
        """Whether this ``(task_id, revision)`` has ever exported successfully.

        The same identity ``put_receipt`` refuses a second write for. Reading it
        first, before the receipt-only side effects of an export, is what lets a
        caller do something *once per delivery* -- write a project-memory fact --
        without a second table to remember whether it already did.
        """
        with self.store.connect() as db:
            row = db.execute(
                'SELECT 1 FROM maintenance_receipts WHERE task_id=? AND revision=?',
                (task_id, revision)).fetchone()
        return row is not None

    def put_receipt(self, task_id, revision, receipt) -> dict:
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO maintenance_receipts VALUES (?,?,?,?)',
                       (task_id, revision, json.dumps(receipt, ensure_ascii=False), now()))
            row = db.execute('SELECT data FROM maintenance_receipts WHERE task_id=? AND revision=?',
                             (task_id, revision)).fetchone()
        return json.loads(row[0])

    def receipts(self, task_id) -> list[dict]:
        with self.store.connect() as db:
            return [{'revision': row['revision'], 'at': row['at'],
                     **json.loads(row['data'])}
                    for row in db.execute(
                        'SELECT revision,data,at FROM maintenance_receipts '
                        'WHERE task_id=? ORDER BY revision', (task_id,))]


def status_of(execution_state) -> str:
    """Map one execution state onto one task state, or refuse."""
    if execution_state is None:
        return 'received'
    mapped = EXECUTION_STATES.get(execution_state)
    if mapped is None:
        raise Conflict(f'执行状态 {execution_state} 无法映射到维护任务状态，请人工核对')
    return mapped


class MaintenanceTasks:
    """The Task port, as the CLI and the web adapter both see it.

    ``execution``, ``repository``, ``evidence`` and ``identity`` are explicit
    ports.  Nothing in this class knows about FastAPI, a request object, or a
    live SDK handle.

    Required port surface:

    * ``execution.submit(task, *, actor)`` -> execution id
      ``execution.status(execution_id)`` -> one of ``EXECUTION_STATES``
      ``execution.events(execution_id, after=0)`` -> ``[{sequence,kind,payload}]``
      ``execution.cost_usd(execution_id)`` -> float
      ``execution.intervene(execution_id, text, *, actor)``
      ``execution.resume(execution_id, *, actor)``
      ``execution.cancel(execution_id, *, actor)``
      ``execution.delivery(execution_id)`` -> ``{commit, checks, unverified,
      working_copy_base_sha, repository, capability_source, synthetic}`` or ``None``
      ``execution.export_artifacts(execution_id)`` -> ``{diff_hash, artifacts}``
      where each artifact is ``{name, bytes}``; the hash covers the exported bytes.
    * ``repository.resolve(project_id, repository, base_sha)`` -> repository
      identity; raises ``ValueError`` when the SHA or repo does not match.
    * ``identity.require(actor, project_id)`` -> ``None``; raises on refusal.
    """

    def __init__(self, store, *, execution, repository, identity, evidence=None):
        self.records = MaintenanceStore(store)
        self.execution = execution
        self.repository = repository
        self.identity = identity
        self.evidence = evidence

    # -- creation ---------------------------------------------------------
    def create(self, request, *, actor) -> dict:
        normalized = normalize(request)
        self.identity.require(actor, normalized['project_id'])
        # Refuse an unresolvable baseline before the key is burned, so a fixed
        # resubmission under the same key is still possible.
        self.repository.resolve(normalized['project_id'], normalized['repository'],
                                normalized['base_sha'])
        key = f"maintenance:{normalized['project_id']}:{normalized['idempotency_key']}"
        record, created = self.records.claim(key, normalized,
                                            content_fingerprint(normalized))
        self._bind_execution(record, actor=actor)
        return self.get(record['id'], actor=actor)

    def _bind_execution(self, record, *, actor) -> dict:
        """Give a claimed task its execution, whether or not this call claimed it.

        Claiming the idempotency key and dispatching the work are two writes, and
        nothing can make them one: the execution lives outside this module. So the
        recoverable shape is for the registration to be able to survive alone and
        for a retry to finish it. Before, only the call that created the record
        submitted; a dispatch failure therefore burned the key and left a task
        that said "received" forever with no execution to resume, cancel or
        reconcile, and every retry took the ``already claimed`` path straight back
        to that same dead record.

        Re-submitting is safe because ``submit`` is keyed on this task's own id:
        the adapter's ``create_run`` dedup returns the execution that already
        exists instead of buying a second one, so the three interrupted windows --
        no execution yet, execution built but never queued, queued but never
        linked here -- all converge on the same execution. A task that already
        carries a reference never submits again, which is what keeps a plain
        repeat import from dispatching twice.

        The actor is the caller finishing the binding, who has just passed the
        same identity check the original submitter passed. Only the first
        successful submit reaches the execution side, so this cannot rewrite an
        existing execution's recorded submitter.
        """
        if record.get('execution_id'):
            return record
        execution_id = self.execution.submit(record, actor=actor)
        return self.records.link(record['id'], {'execution_id': execution_id})

    def revise(self, task_id, request, *, actor) -> dict:
        """Reopen/update: a new revision that points back, never an overwrite.

        The predecessor's receipts stay where they are; the successor earns its
        own.  A revision carrying byte-identical content is still refused,
        because "reopened with nothing new" is a mistake worth surfacing.
        """
        previous = self.records.get(task_id)
        self.identity.require(actor, previous['project_id'])
        normalized = normalize(request)
        if normalized['project_id'] != previous['project_id']:
            raise Conflict('新修订必须属于同一个项目')
        fingerprint = content_fingerprint(normalized)
        if fingerprint == previous['content_fingerprint']:
            raise Conflict('新修订与原任务内容完全相同，无需重新受理')
        self.repository.resolve(normalized['project_id'], normalized['repository'],
                                normalized['base_sha'])
        key = f"maintenance:{normalized['project_id']}:{normalized['idempotency_key']}"
        record, created = self.records.claim(key, normalized, fingerprint,
                                            predecessor=previous)
        self._bind_execution(record, actor=actor)
        return self.get(record['id'], actor=actor)

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
        """The typed shape M4 renders. Every field is derived or recorded, none guessed."""
        execution_id = record.get('execution_id')
        status = status_of(self.execution.status(execution_id) if execution_id else None)
        if status in ('running', 'waiting') and record.get('cancel_requested'):
            status = 'cancelling'
        delivery = self.execution.delivery(execution_id) if execution_id else None
        receipts = self.records.receipts(record['id'])
        return {
            'schema_version': SCHEMA_VERSION,
            'task_id': record['id'], 'revision': record['revision'],
            'predecessor_id': record.get('predecessor_id'),
            'successor_id': record.get('successor_id'),
            'status': status,
            'issue': record['issue'], 'issue_digest': record['issue_digest'],
            'project_id': record['project_id'],
            'baseline': {'repository': record['repository'],
                         'base_sha': record['base_sha'],
                         'base_branch_label': record['base_branch_label']},
            'agreement': record['agreement'],
            'expected_behaviour': record['expected_behaviour'],
            'delivery_goal': record['delivery_goal'],
            'delivery_tier': record['delivery_tier'],
            'synthetic': record.get('synthetic', False),
            'execution_id': execution_id,
            'cost_usd': self.execution.cost_usd(execution_id) if execution_id else 0.0,
            'blocking_reason': self._blocking_reason(status, execution_id),
            'steps': self._steps(status, delivery, receipts),
            'delivery': delivery,
            'receipts': receipts,
            'created_at': record['created_at'],
            'project_memory': self._project_memory(record['project_id']),
        }

    def _project_memory(self, project_id) -> list[dict]:
        """What this task's project currently knows, in the data rather than only
        in a past prompt.

        This is the same cross-scenario read ``WebuddyExecution.submit`` makes
        before dispatch (``scenario_memory.recall`` with no ``plugin_id``), so a
        person or a later task looking at this task's view sees what the project
        remembers without having to go re-read an old executor prompt for it.
        It is read live, and the field is named for that: it is what the project
        knows *now*, not the set this task's dispatch actually carried. The two
        differ whenever a constraint is confirmed after dispatch, and calling it
        a reference would be a claim about the past that nothing here can back.
        The receipt deliberately does not carry this field for the same reason --
        a frozen delivery record must not quote a value that keeps moving.

        A project with no memory yet, or knowledge that failed to load, renders
        as an empty list: reading what the project remembers must not be a way
        to break reading the task itself.
        """
        from factory.control import scenario_memory
        try:
            entries = scenario_memory.recall(self.records.store, project_id)
        except (KeyError, ValueError):
            return []
        return [{'key': entry['key'], 'title': entry['title'],
                 'status': entry['status'], 'kind': entry['kind'],
                 'paths': entry['paths'], 'commit_sha': entry.get('commit_sha'),
                 'revision': entry['revision']} for entry in entries]

    #: Event kinds that explain a stop, sharing the vocabulary the existing
    #: attention surface already reads (``autonomy_routes`` treats ``run.recovered``
    #: as the reason a recovered run is waiting). Leaving it out did not make this
    #: module silent -- it made it say "no citable reason" while the reason sat in
    #: the log, which is worse than saying nothing.
    BLOCKING_EVENTS = ('run.failed', 'run.blocked', 'run.recovered',
                       'clarification.requested', 'budget.exhausted',
                       'approval.requested', 'requirement_analysis.interrupted')

    #: I1..I7 of the issue-maintenance SOP, in order. A step is ``done`` only on
    #: evidence the execution port actually reports; there is no timer anywhere
    #: in this module, and a step never advances because time passed.
    STEPS = ('intake', 'triage', 'reproduce', 'modify', 'verify', 'deliver',
             'receipt')

    def _steps(self, status, delivery, receipts=()) -> list[dict]:
        checks = (delivery or {}).get('checks') or []
        reached = {
            'received': 'intake', 'running': 'modify', 'waiting': 'triage',
            'cancelling': 'modify', 'failed': 'modify',
            'cancelled': 'intake', 'delivered': 'receipt',
        }[status]
        if status == 'delivered':
            reached = 'receipt' if (delivery or {}).get('commit') else 'verify'
        if status == 'running' and checks:
            reached = 'verify'
        # A step nobody is working on must not render the same as one in flight.
        # ``blocked`` waits on a human, ``stopped`` is over.
        halted = {'failed': 'stopped', 'cancelled': 'stopped',
                  'cancelling': 'stopped', 'waiting': 'blocked'}.get(status)
        index = self.STEPS.index(reached)
        steps = []
        for position, name in enumerate(self.STEPS):
            state = ('done' if position < index else
                     'current' if position == index else 'pending')
            if halted and position == index:
                state = halted
            steps.append({'step': name, 'state': state})
        # The last step has nothing after it to make it ``done``, so without this
        # a delivered task with its receipt already written renders as forever
        # in progress. The receipt existing is the evidence, not the status.
        if reached == self.STEPS[-1] and receipts and not halted:
            steps[-1]['state'] = 'done'
        return steps

    def _blocking_reason(self, status, execution_id) -> dict | None:
        """Why a human is needed, with the event that says so -- or nothing.

        A surface that renders an empty reason for a waiting task teaches its
        reader to ignore the field, so the reason and its event reference are
        produced together or not at all.
        """
        if status not in ('waiting', 'failed') or not execution_id:
            return None
        for event in reversed(self.execution.events(execution_id)):
            if event['kind'] in self.BLOCKING_EVENTS:
                payload = event.get('payload') or {}
                return {'kind': event['kind'],
                        'message': str(payload.get('message')
                                       or payload.get('question') or '')[:2000],
                        'event': {'execution_id': execution_id,
                                  'sequence': event['sequence']}}
        return {'kind': 'unknown', 'message': '执行已停下但没有留下可引用的原因事件，需人工核对',
                'event': None}

    # -- intervention -----------------------------------------------------
    def events(self, task_id, *, actor, after=0) -> list[dict]:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        if not record.get('execution_id'):
            return []
        return self.execution.events(record['execution_id'], after=after)

    def intervene(self, task_id, text, *, actor) -> dict:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        if not record.get('execution_id'):
            raise Conflict('这个维护任务还没有绑定执行，无法补充信息')
        self.execution.intervene(record['execution_id'], text, actor=actor)
        return self.get(task_id, actor=actor)

    def resume(self, task_id, *, actor) -> dict:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        if not record.get('execution_id'):
            raise Conflict('这个维护任务还没有绑定执行，无法恢复')
        self.execution.resume(record['execution_id'], actor=actor)
        return self.get(task_id, actor=actor)

    def cancel(self, task_id, *, actor) -> dict:
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        # Record the intent first. If the execution-side cancel is what crashes,
        # a reopened process still knows a human asked for this.
        self.records.link(task_id, {'cancel_requested': True})
        if record.get('execution_id'):
            self.execution.cancel(record['execution_id'], actor=actor)
        return self.get(task_id, actor=actor)

    # -- delivery ---------------------------------------------------------
    def export(self, task_id, *, actor) -> dict:
        """Freeze the receipt for a finished task and return it with its artifacts.

        Called twice, this returns the first receipt: the table refuses an update
        for this ``(task_id, revision)``, so a second export cannot quietly
        restate a delivery under new numbers.
        """
        record = self.records.get(task_id)
        self.identity.require(actor, record['project_id'])
        view = self._view(record)
        if view['status'] != 'delivered':
            raise Conflict(f"维护任务当前状态为 {view['status']}，还没有可交付的结果")
        delivery = view['delivery'] or {}
        if not delivery.get('commit'):
            raise Conflict('执行没有报告交付 commit，不能出回执')
        # The baseline in the receipt is the one the execution actually worked on,
        # cross-checked against what was pinned at intake. A receipt that quoted
        # only the registry would be true about the form and silent about the run.
        actual = delivery.get('working_copy_base_sha')
        if actual != record['base_sha']:
            raise Conflict('执行工作副本的基线与登记的基线不一致，拒绝出回执：'
                           f"登记 {record['base_sha']}，实际 {actual}")
        if delivery.get('repository') != record['repository']:
            raise Conflict('执行工作副本的仓库与登记的仓库不一致，拒绝出回执')
        # The patch and its hash come out of one call, so the hash in the receipt
        # is a hash *of the bytes that were exported* rather than a second,
        # independently computed number that can disagree with them.
        exported = self.execution.export_artifacts(view['execution_id'])
        receipt = self.receipt(view, delivery, exported)
        # Checked before the receipt is written, and the write that follows is
        # the only thing gated on it: a second export of the same delivery must
        # not write a second memory entry, and ``put_receipt``'s own append-only
        # guarantee is what "same (task_id, revision)" already means here, so
        # this reuses that identity instead of a second ledger of its own.
        if not self.records.has_receipt(task_id, record['revision']):
            self._remember_delivery(record, delivery, exported, actor=actor)
        stored = self.records.put_receipt(task_id, record['revision'], receipt)
        return {'receipt': stored, 'text': render_receipt(stored),
                'artifacts': exported['artifacts']}

    def _remember_delivery(self, record, delivery, exported, *, actor) -> None:
        """Write back what this delivery actually did, as an unconfirmed fact.

        Called once per delivery (see ``has_receipt`` above), so the next task
        against this project -- in this session or a new one -- can read what
        happened here without re-deriving it from a diff. ``status`` is always
        ``candidate``: this module observed the delivery, nobody confirmed it is
        the right long-term behaviour, and only a human review can promote it to
        ``active``. Ordered before the receipt is committed on purpose: if this
        raises, the receipt is not written either, so a retry of the same export
        call tries the memory write again instead of silently never happening
        because ``has_receipt`` now says "already done".
        """
        from factory.control import scenario_memory
        issue = record['issue']
        topic = issue['title'] or f"{issue['source']}#{issue['external_id']}"
        checks = delivery.get('checks') or []
        check_summary = ('、'.join(
            f"{check.get('name')}{'通过' if check.get('passed') else '未通过'}"
            for check in checks) or '（本次无检查记录）')
        content = '\n'.join([
            f"处理了 Issue {issue['source']}#{issue['external_id']} v{issue['version']}："
            f"{issue['title']}",
            f"期望行为：{record['expected_behaviour']}",
            f"交付目标：{record['delivery_goal']}",
            f"检查结果：{check_summary}",
            '未验证项：' + ('、'.join(delivery.get('unverified') or []) or '（无）'),
        ])
        # Neither the issue title nor the unverified list has a length cap of its
        # own before this point, but the knowledge store's ``title``/``content``
        # fields do. Truncating here, rather than letting that call raise, is
        # what keeps an unusually long issue from being the reason a *correct*
        # delivery cannot be exported at all.
        scenario_memory.record(
            self.records.store, record['project_id'], plugin_id='issue-maintenance',
            topic=_bounded(topic, _MEMORY_TOPIC_MAX),
            content=_bounded(content, _MEMORY_CONTENT_MAX),
            actor=(actor or {}).get('username') or 'issue-maintenance',
            kind='fact', status=scenario_memory.UNCONFIRMED_STATUS,
            paths=paths_from_patch(exported.get('artifacts')),
            commit_sha=delivery.get('commit'))

    @staticmethod
    def receipt(view, delivery, exported) -> dict:
        """The machine-readable receipt. Unverified items are part of it, not a footnote."""
        return {
            'schema': 'webuddy.maintenance.receipt/1',
            'task_id': view['task_id'], 'revision': view['revision'],
            'issue': {**view['issue'], 'digest': view['issue_digest']},
            'baseline': view['baseline'],
            'delivery': {'commit': delivery.get('commit'),
                         'diff_hash': exported['diff_hash'],
                         'artifacts': [a['name'] for a in exported['artifacts']]},
            'agreement': view['agreement'],
            'checks': [{'name': check.get('name'), 'passed': check.get('passed'),
                        'exit_code': check.get('exit_code'),
                        # Applicability travels with the result. A reused pass is
                        # still a pass, but a receipt that hides which round it was
                        # earned in is a receipt a reader cannot audit.
                        'reused': bool(check.get('reused')),
                        'identity_fingerprint': check.get('identity_fingerprint')}
                       for check in (delivery.get('checks') or [])],
            'unverified': list(delivery.get('unverified') or []),
            'delivery_tier': view['delivery_tier'],
            'capability_source': delivery.get('capability_source') or 'unspecified',
            # Either side saying "synthetic" is enough to carry the disclaimer:
            # the import declared it, or the execution reported it. Dropping the
            # label needs both to be silent, so the failure direction is an
            # unnecessary disclaimer rather than a demo that reads as a customer
            # success.
            'synthetic': bool(view.get('synthetic') or delivery.get('synthetic')),
        }


def render_receipt(receipt) -> str:
    """The human-readable half of the same receipt, from the same data."""
    lines = [
        f"维护回执 {receipt['task_id']} 修订 {receipt['revision']}",
        f"原 Issue：{receipt['issue']['source']}#{receipt['issue']['external_id']}"
        f" v{receipt['issue']['version']}（摘要 {receipt['issue']['digest'][:12]}）",
        f"  标题：{receipt['issue']['title']}",
        f"基线：{receipt['baseline']['repository']} @ {receipt['baseline']['base_sha']}"
        + (f"（分支标注 {receipt['baseline']['base_branch_label']}）"
           if receipt['baseline']['base_branch_label'] else ''),
        f"交付：commit {receipt['delivery']['commit']}"
        f"，diff {receipt['delivery']['diff_hash']}",
        f"  构建物：{'、'.join(receipt['delivery']['artifacts']) or '（无）'}",
        f"适用约定版本：{receipt['agreement']['revision']}"
        + (f"（Skill {receipt['agreement']['skill_version']}）"
           if receipt['agreement'].get('skill_version') else ''),
        '检查结果：',
    ]
    for check in receipt['checks'] or [{'name': '（无）', 'passed': None, 'exit_code': None}]:
        verdict = '通过' if check['passed'] else '未通过' if check['passed'] is False else '未运行'
        # Said out loud: a reader must be able to tell a check that ran this round
        # from a saved result carried forward, and must be told when the scope of
        # that reuse cannot be identified.
        if check.get('reused'):
            scope = (f"复用既有结果，证据身份 {check['identity_fingerprint'][:12]}"
                     if check.get('identity_fingerprint')
                     else '复用既有结果，但没有记录证据身份，适用范围无法核对')
        else:
            scope = '本轮实际运行'
        lines.append(f"  - {check['name']}：{verdict}（退出码 {check['exit_code']}）"
                     f"，{scope}")
    lines.append('未验证项：')
    for item in receipt['unverified'] or ['（无显式未验证项）']:
        lines.append(f'  - {item}')
    tier = {'package': '交包待发布，本次未部署到任何环境',
            'authorized_test_project': '已部署到明确授权的公司测试项目'}
    lines.append('部署层级：' + tier.get(receipt['delivery_tier'], receipt['delivery_tier']))
    lines.append('实际能力来源：' + receipt['capability_source'])
    if receipt['synthetic']:
        lines.append('本次交付基于合成示例仓库，不代表任何客户成功案例。')
    return '\n'.join(lines)
