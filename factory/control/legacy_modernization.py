"""Legacy modernization (信创化改造): a target registered once, a bounded slice
delivered for real, revisions that keep the chain.

Shape mirrors ``factory/control/issue_maintenance.py`` deliberately: one Store
with append-only receipts, one Port with explicit ``execution``/``repository``/
``identity`` ports, no locally-owned status column -- a slice's status is
derived from the execution port on every read.  The maintenance module is not
imported for its business logic (a different ``SOURCE_TYPE`` and a different
prompt), but its two host-boundary adapters (project authorization, pinned-
baseline resolution against the real checkout) are generic across every
scenario and are reused rather than rewritten a second time.

What this module owns:

* The modernization target, as project memory. There is no separate "target"
  table -- ``scenario_memory`` already carries source/status/scope/version, and
  a second table would be a second, competing answer to "what does this
  project want". A dimension is written ``candidate`` (a report's suggestion)
  until ``confirm_dimension`` promotes it, with its own reviewer, to ``active``
  (a customer decision). That distinction is the one SHARED.md and B0-REUSE.md
  both call out by name: an evaluation report proposing Java17/SpringBoot3/达梦
  is not the same fact as a customer approving it.
* A disposal/slice plan, assembled by asking CodeGraph (or, when no index
  exists yet, reporting that plainly rather than guessing) where each
  dimension's concerns actually live in the repository, tagged with the commit
  the answer came from.
* One migration slice per bounded change, dispatched through the same
  ``execute_plan`` executor every other business surface uses. A dimension
  whose real target environment cannot be exercised here (no 达梦 instance, no
  target JDK image, no CA SDK) does not get to claim "verified": the receipt
  says so explicitly, derived from which checks actually ran rather than typed
  by whoever writes the receipt.

Deliberate non-goals, matching ``issue_maintenance.py``'s list for the same
reasons:

* No status column, no second executor/budget/queue, no SQL against
  ``runs``/``events``, no frontend session import.
* No new project-memory table, no new CodeGraph implementation -- both are
  reached through their one existing entry point.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

from factory.control import code_intel, codegraph, scenario_memory
from factory.control.issue_maintenance_webuddy import WebuddyIdentity, WebuddyRepository
from factory.control.store import Conflict, now

SCHEMA_VERSION = 1

#: The plugin this module's business surface belongs to.
PLUGIN_ID = 'legacy-modernization'

#: Deliberately not ``issue_maintenance`` -- a run's source type is how the
#: execution port refuses to read another module's evidence as its own, so
#: reusing the maintenance value here would let a modernization slice render a
#: maintenance run's delivery (or vice versa) as its own.
SOURCE_TYPE = 'legacy_modernization'

#: The real dimensions a 信创 target expands into, per the task brief:
#: database/version, OS/CPU, JDK/framework, browser, CA/signature, external
#: components. A dimension not in this tuple is refused rather than filed
#: under a made-up bucket nobody will ever query back out.
DIMENSIONS = ('database', 'os_cpu', 'jdk_framework', 'browser', 'ca_signature',
              'external_component')

DIMENSION_LABELS = {
    'database': '数据库/版本',
    'os_cpu': 'OS/CPU',
    'jdk_framework': 'JDK/框架',
    'browser': '浏览器',
    'ca_signature': 'CA/签章',
    'external_component': '外部组件',
}

#: Heuristic terms used both to search CodeGraph for a dimension's concerns and
#: to decide, at export time, whether a delivered check plausibly exercised
#: that dimension for real. Heuristic in both directions on purpose: a search
#: hit is a lead, not proof, and a check-name match is evidence of coverage,
#: not proof of a passing real-environment run -- the pass/fail bit still comes
#: from the check's own exit code.
DIMENSION_HINTS = {
    'database': ('jdbc', 'datasource', 'database', 'dm', 'sql'),
    'os_cpu': ('os.', 'arch', 'native', 'platform'),
    'jdk_framework': ('springboot', 'spring', 'servlet', 'jdk'),
    'browser': ('activex', 'ie11', 'webview', 'browser'),
    'ca_signature': ('certificate', 'keystore', 'ca_sign', 'signature'),
    'external_component': ('sdk', 'gateway', 'external_client'),
}

#: Where a delivery may land. Same vocabulary as issue-maintenance, because it
#: describes the host's delivery boundary, not a maintenance-specific concept.
DELIVERY_TIERS = ('package', 'authorized_test_project')

_SHA = re.compile(r'^[0-9a-f]{40}$')
_KEY = re.compile(r'^[A-Za-z0-9_-]{8,100}$')

_REQUIRED = ('project_id', 'repository', 'base_sha', 'dimension', 'scope_paths',
             'expected_behaviour', 'delivery_goal', 'agreement', 'idempotency_key')


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Target = project memory, not a second table
# ---------------------------------------------------------------------------

def _dimension_topic(dimension: str) -> str:
    return f'目标栈：{DIMENSION_LABELS[dimension]}'


def _dimension_of(title: str) -> str | None:
    for dim, label in DIMENSION_LABELS.items():
        if title.endswith(f'目标栈：{label}'):
            return dim
    return None


def register_target(store, project_id, target, *, actor, source='', paths=(),
                     commit_sha=None) -> list[dict]:
    """Record what an evaluation proposes for each declared dimension.

    Always writes ``candidate``/``hypothesis``: an evaluation report's
    suggestion is not a customer decision, and this function has no way to
    tell whether the caller is quoting a report or repeating something a
    customer already said out loud -- so it never guesses ``active``.
    ``confirm_dimension`` is the one path that can, because it is the one path
    a human reviewer actually calls.
    """
    if not isinstance(target, dict) or not target:
        raise ValueError('必须至少给出一个改造目标维度')
    unknown = set(target) - set(DIMENSIONS)
    if unknown:
        raise ValueError('未知的改造维度：' + '、'.join(sorted(unknown)))
    existing = {e['title']: e for e in
                scenario_memory.recall(store, project_id, plugin_id=PLUGIN_ID)}
    recorded = []
    for dimension, value in target.items():
        value = str(value or '').strip()
        if not value:
            continue
        topic = _dimension_topic(dimension)
        title = scenario_memory.title_for(PLUGIN_ID, topic)
        prior = existing.get(title)
        content = f'{value}（来源：{source}）' if source else value
        entry = scenario_memory.record(
            store, project_id, plugin_id=PLUGIN_ID, topic=topic, content=content,
            actor=actor, kind='hypothesis', status=scenario_memory.UNCONFIRMED_STATUS,
            paths=paths, commit_sha=commit_sha,
            key=prior['key'] if prior else None,
            expected_revision=prior['revision'] if prior else 0)
        recorded.append(entry)
    if not recorded:
        raise ValueError('提交的目标维度全部为空')
    return recorded


def confirm_dimension(store, project_id, dimension, *, actor) -> dict:
    """Promote one dimension's current statement from candidate to active.

    This is the one place a report's suggestion becomes a requirement the rest
    of the plugin may act on. It re-records the *same* content under
    ``kind='decision'``/``status='active'``, using the entry's own current
    key/revision as the compare-and-swap, so a stale confirmation racing a
    fresh registration is refused by ``KnowledgeStore`` rather than silently
    overwritten.
    """
    if dimension not in DIMENSIONS:
        raise ValueError('未知的改造维度：' + dimension)
    entries = scenario_memory.recall(store, project_id, plugin_id=PLUGIN_ID)
    title = scenario_memory.title_for(PLUGIN_ID, _dimension_topic(dimension))
    current = next((e for e in entries if e['title'] == title), None)
    if current is None:
        raise Conflict(f'{DIMENSION_LABELS[dimension]}还没有登记任何目标，无法确认')
    if current['status'] == scenario_memory.CONFIRMED_STATUS:
        return current
    return scenario_memory.record(
        store, project_id, plugin_id=PLUGIN_ID, topic=_dimension_topic(dimension),
        content=current['content'], actor=actor, kind='decision',
        status=scenario_memory.CONFIRMED_STATUS, paths=current.get('paths', ()),
        commit_sha=current.get('commit_sha'), key=current['key'],
        expected_revision=current['revision'])


def dimensions_view(store, project_id) -> list[dict]:
    """Every dimension's current statement, or ``missing`` when none exists yet.

    A missing dimension only blocks a slice that names it; it never blocks this
    read, which is the whole point of exposing it separately from a slice.
    """
    entries = scenario_memory.recall(store, project_id, plugin_id=PLUGIN_ID)
    by_dim = {}
    for entry in entries:
        dim = _dimension_of(entry['title'])
        if dim:
            by_dim[dim] = entry
    result = []
    for dim in DIMENSIONS:
        entry = by_dim.get(dim)
        result.append({
            'dimension': dim, 'label': DIMENSION_LABELS[dim],
            'status': entry['status'] if entry else 'missing',
            'content': entry['content'] if entry else None,
            'confirmed': bool(entry and entry['status'] == scenario_memory.CONFIRMED_STATUS),
            'paths': entry.get('paths', []) if entry else [],
            'commit_sha': entry.get('commit_sha') if entry else None,
        })
    return result


# ---------------------------------------------------------------------------
# Dependency disposal + slice list, code-located with a source
# ---------------------------------------------------------------------------

def locate(store, project_id, query, *, limit=10, language=None) -> dict:
    """Where this term lives, with the source and the freshness it came from.

    Two backends, and the answer always says which one replied:

    ``code_intel`` -- the shared multi-language layer (``factory/control/code_intel.py``).
    This is the one that can answer for a Java or C# customer repository, and it
    reads the working tree, so uncommitted code counts.

    ``codegraph-legacy`` -- the small in-tree indexer, used only when the shared
    layer cannot answer. It parses Python and JS/TS **only** and reads a fixed
    commit, so a Java lookup that falls through to it comes back empty for a
    reason that has nothing to do with the code. That is why the fallback's
    empty answer carries ``covers`` and a note rather than being reported as
    "not found" -- a language this indexer never reads must not look like a
    language with no matches.

    Neither path fabricates a location. No usable index is reported as exactly
    that, per SHARED.md's 「缺工具时可用符号/文本检索继续工作，不能捏造图查询结果」.
    """
    project = store.project(project_id)
    workspace = project.get('workspace')
    fallback_reason = None
    if workspace and Path(workspace).is_dir():
        # A read does not build an index: that is minutes of work and a write
        # into the customer's checkout. Indexing is the explicit code-index action.
        answer = code_intel.definitions(workspace, query, language=language,
                                        limit=limit, auto_index=False)
        if answer['outcome'] in (code_intel.OK, code_intel.TRUNCATED,
                                 code_intel.NO_MATCH):
            return {
                'source': 'code_intel',
                'outcome': answer['outcome'],
                'truncated': answer['truncated'],
                'fresh': answer['freshness']['stale'] is False,
                'commit_sha': answer['code_version']['commit'],
                'dirty': answer['code_version']['dirty'],
                'language': language,
                'languages_indexed': answer['freshness'].get('languages') or [],
                'backend': {k: answer['backend'].get(k)
                            for k in ('package', 'version', 'available')},
                'unresolved': answer['unresolved'],
                'dropped_other_language': answer['dropped_other_language'],
                'results': [{'path': r['path'], 'line': r['line'],
                             'end_line': r['end_line'], 'name': r['name'],
                             'kind': r['kind'], 'language': r['language'],
                             'resolution': 'index'} for r in answer['results']],
                'note': answer['reason'],
            }
        fallback_reason = answer['reason']

    try:
        snapshot = codegraph.get_snapshot(store, project_id)
    except ValueError:
        # No usable checkout to resolve a baseline from (missing workspace, bad
        # branch, ...). Reported the same way as "never indexed": a location a
        # reader must fall back to text/manual search for, not a crash.
        snapshot = None
    if snapshot is None:
        return {'source': 'none', 'outcome': code_intel.NOT_INDEXED, 'fresh': False,
                'truncated': False, 'commit_sha': None, 'results': [],
                'language': language, 'languages_indexed': [],
                'unresolved': list(code_intel.CROSS_BOUNDARY_UNRESOLVED),
                'note': (fallback_reason or '尚未建立代码索引') +
                        '；建立索引用 POST /api/v2/projects/{id}/code-index。'
                        '在此之前的代码定位只能人工核对，不能当作已验证的定位'}
    try:
        fresh = snapshot['commit_sha'] == codegraph.baseline_sha(project)
    except ValueError:
        fresh = False
    results = codegraph.search_snapshot(snapshot, query, limit=limit)
    return {'source': 'codegraph-legacy', 'outcome':
                code_intel.OK if results else code_intel.NO_MATCH,
            'fresh': fresh, 'truncated': False,
            'commit_sha': snapshot['commit_sha'], 'language': language,
            # Said out loud, because an empty answer from here means "this
            # indexer does not read that language", not "the symbol is absent".
            'covers': ['python', 'javascript', 'typescript'],
            'languages_indexed': ['python', 'javascript', 'typescript'],
            'unresolved': list(code_intel.CROSS_BOUNDARY_UNRESOLVED),
            'note': (fallback_reason or '共享代码查询层没有应答') +
                    '；这里用的是仓库内的轻量索引，只解析 Python 与 JS/TS，'
                    '且只读已提交的那个 commit。其它语言的空结果不代表代码里没有',
            'results': [{'path': r['path'], 'line': r['line'], 'end_line': r['end_line'],
                        'name': r['name'], 'kind': r['kind'], 'language': None,
                        'resolution': r['resolution']} for r in results]}


def disposal_plan(store, project_id, *, queries=None) -> list[dict]:
    """One entry per dimension: its target status and where it likely lives.

    ``blocked`` marks a dimension whose target is not yet ``active`` -- a slice
    that depends on it may still be drafted, but should not claim the
    dependency is settled. This is what keeps a missing/unconfirmed dimension
    from stalling the whole round: everything else in the plan is unaffected.
    """
    dims = {d['dimension']: d for d in dimensions_view(store, project_id)}
    queries = queries or {}
    plan = []
    for dim in DIMENSIONS:
        info = dims[dim]
        terms = queries.get(dim) or DIMENSION_HINTS[dim]
        locations = [{'query': term, **locate(store, project_id, term, limit=5)}
                     for term in terms]
        if info['status'] == 'missing':
            reason = '目标尚未登记，以下定位仅供起草参考'
        elif info['status'] != scenario_memory.CONFIRMED_STATUS:
            reason = '目标尚未人工确认，仅供参考，不作为切片依据'
        else:
            reason = None
        plan.append({
            'dimension': dim, 'label': DIMENSION_LABELS[dim],
            'target_status': info['status'], 'target_content': info['content'],
            'blocked': reason is not None, 'blocked_reason': reason,
            'locations': locations,
        })
    return plan


# ---------------------------------------------------------------------------
# One migration slice: normalize / store / port
# ---------------------------------------------------------------------------

def normalize(request) -> dict:
    """Validate a slice request and return its canonical form.

    ``scope_paths`` is required and must be non-empty: "对一条范围明确的切片"
    is the brief's own words, and a slice with no declared scope is exactly the
    "migrate the whole repository" shape this module refuses to accept.
    """
    if not isinstance(request, dict):
        raise ValueError('改造切片请求必须是一个对象')
    missing = [key for key in _REQUIRED if key != 'scope_paths' and not request.get(key)]
    if 'scope_paths' not in request:
        missing.append('scope_paths')
    if missing:
        raise ValueError('改造切片缺少必填项：' + '、'.join(missing))
    dimension = request['dimension']
    if dimension not in DIMENSIONS:
        raise ValueError('改造维度只能是：' + '、'.join(DIMENSIONS))
    scope_paths = request['scope_paths']
    if (not isinstance(scope_paths, list) or not scope_paths or
            not all(isinstance(p, str) and p.strip() for p in scope_paths)):
        raise ValueError('必须给出至少一个具体的范围路径，不能是整仓库')
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
    known_gaps = request.get('known_gaps') or []
    if not isinstance(known_gaps, list) or not all(isinstance(g, str) for g in known_gaps):
        raise ValueError('已知未验证条件必须是字符串列表')
    raw_locations = request.get('code_locations') or []
    if not isinstance(raw_locations, list):
        raise ValueError('code_locations 必须是列表')
    locations = []
    for item in raw_locations:
        if not isinstance(item, dict) or not item.get('path'):
            raise ValueError('code_locations 的每一项必须包含 path')
        line = item.get('line')
        locations.append({'path': str(item['path']),
                          'line': int(line) if line else None,
                          'source': str(item.get('source') or 'manual')})
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': str(request['project_id']),
        'repository': str(request['repository']),
        'base_sha': str(request['base_sha']),
        'base_branch_label': str(request.get('base_branch_label') or ''),
        'dimension': dimension,
        'scope_paths': [str(p).strip() for p in scope_paths],
        'expected_behaviour': str(request['expected_behaviour']),
        'delivery_goal': str(request['delivery_goal']),
        'agreement': {'revision': str(agreement['revision']),
                      'skill_version': str(agreement.get('skill_version') or '')},
        'idempotency_key': str(request['idempotency_key']),
        'delivery_tier': tier,
        'known_gaps': [str(g) for g in known_gaps],
        'code_locations': locations,
        # Same reasoning as issue_maintenance.normalize: declared at import
        # time so a real delivery cannot acquire the disclaimer by omission.
        'synthetic': bool(request.get('synthetic')),
    }


def content_fingerprint(normalized) -> str:
    return _digest({key: normalized[key] for key in (
        'project_id', 'repository', 'base_sha', 'dimension', 'scope_paths',
        'expected_behaviour', 'delivery_goal', 'agreement', 'delivery_tier',
        'known_gaps', 'code_locations', 'synthetic')})


class ModernizationStore:
    """Module-owned tables, same append-only-receipt shape as ``MaintenanceStore``."""

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS modernization_slices(
                id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS modernization_slice_keys(
                key TEXT PRIMARY KEY, slice_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS modernization_receipts(
                slice_id TEXT NOT NULL, revision INTEGER NOT NULL,
                data TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(slice_id, revision));
            CREATE TRIGGER IF NOT EXISTS modernization_receipts_no_update
                BEFORE UPDATE ON modernization_receipts BEGIN
                SELECT RAISE(ABORT, 'modernization receipts are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS modernization_receipts_no_delete
                BEFORE DELETE ON modernization_receipts BEGIN
                SELECT RAISE(ABORT, 'modernization receipts are append-only');
            END;
            ''')

    def get(self, slice_id) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM modernization_slices WHERE id=?',
                             (slice_id,)).fetchone()
        if row is None:
            raise KeyError(slice_id)
        return json.loads(row[0])

    def slices(self, *, project_id=None) -> list[dict]:
        with self.store.connect() as db:
            records = [json.loads(row[0]) for row in
                       db.execute('SELECT data FROM modernization_slices')]
        if project_id is not None:
            records = [r for r in records if r['project_id'] == project_id]
        return sorted(records, key=lambda r: r['created_at'])

    def claim(self, key, normalized, fingerprint, *, predecessor=None) -> tuple[dict, bool]:
        slice_id = uuid.uuid4().hex
        record = {'id': slice_id,
                  'revision': (predecessor['revision'] + 1) if predecessor else 1,
                  'predecessor_id': predecessor['id'] if predecessor else None,
                  'successor_id': None, 'execution_id': None,
                  'cancel_requested': False, 'created_at': now(),
                  'content_fingerprint': fingerprint, **normalized}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT slice_id,fingerprint FROM modernization_slice_keys WHERE key=?',
                             (key,)).fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint:
                    raise Conflict('这个改造切片已被接收；内容发生变化，请用新的幂等键重新提交。')
                existing = db.execute('SELECT data FROM modernization_slices WHERE id=?',
                                      (row['slice_id'],)).fetchone()
                if existing is None:
                    raise Conflict('已登记的改造切片记录不可用，请刷新列表')
                return json.loads(existing[0]), False
            db.execute('INSERT INTO modernization_slice_keys VALUES (?,?,?)',
                       (key, slice_id, fingerprint))
            db.execute('INSERT INTO modernization_slices VALUES (?,?)',
                       (slice_id, json.dumps(record, ensure_ascii=False)))
            if predecessor is not None:
                prior = db.execute('SELECT data FROM modernization_slices WHERE id=?',
                                   (predecessor['id'],)).fetchone()
                if prior is None:
                    raise KeyError(predecessor['id'])
                old = json.loads(prior[0])
                if old.get('successor_id'):
                    raise Conflict('这个改造切片已经有一个后继修订，请在最新修订上继续')
                old['successor_id'] = slice_id
                db.execute('UPDATE modernization_slices SET data=? WHERE id=?',
                           (json.dumps(old, ensure_ascii=False), predecessor['id']))
        return record, True

    def link(self, slice_id, changes) -> dict:
        allowed = {'execution_id', 'successor_id', 'cancel_requested'}
        if set(changes) - allowed:
            raise ValueError('改造切片只允许补记执行引用、后继与取消意图')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM modernization_slices WHERE id=?',
                             (slice_id,)).fetchone()
            if row is None:
                raise KeyError(slice_id)
            record = json.loads(row[0])
            if 'execution_id' in changes and record.get('execution_id') not in (
                    None, changes['execution_id']):
                raise Conflict('这个改造切片已经绑定了一次执行，不能改绑到另一次执行')
            record.update(changes)
            db.execute('UPDATE modernization_slices SET data=? WHERE id=?',
                       (json.dumps(record, ensure_ascii=False), slice_id))
        return record

    def put_receipt(self, slice_id, revision, receipt) -> dict:
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO modernization_receipts VALUES (?,?,?,?)',
                       (slice_id, revision, json.dumps(receipt, ensure_ascii=False), now()))
            row = db.execute('SELECT data FROM modernization_receipts WHERE slice_id=? AND revision=?',
                             (slice_id, revision)).fetchone()
        return json.loads(row[0])

    def receipts(self, slice_id) -> list[dict]:
        with self.store.connect() as db:
            return [{'revision': row['revision'], 'at': row['at'], **json.loads(row['data'])}
                    for row in db.execute(
                        'SELECT revision,data,at FROM modernization_receipts '
                        'WHERE slice_id=? ORDER BY revision', (slice_id,))]


#: Execution-side vocabulary mapped onto ours. Same source vocabulary as the
#: run store publishes everywhere else; an unmapped state is refused rather
#: than guessed, for the same reason ``issue_maintenance.status_of`` refuses.
EXECUTION_STATES = {
    'received': 'received', 'queued': 'running', 'planning': 'running',
    'running': 'running', 'verifying': 'running',
    'needs_human': 'waiting', 'awaiting_approval': 'waiting',
    # A real model asks questions. Leaving this state unmapped made the whole
    # task view raise 409 the moment it did -- not "stuck", *unreadable*.
    'needs_clarification': 'waiting',
    'ready_for_review': 'delivered', 'published': 'delivered',
    'failed': 'failed', 'cancelled': 'cancelled', 'discarded': 'cancelled',
}


def status_of(execution_state) -> str:
    if execution_state is None:
        return 'received'
    mapped = EXECUTION_STATES.get(execution_state)
    if mapped is None:
        raise Conflict(f'执行状态 {execution_state} 无法映射到改造切片状态，请人工核对')
    return mapped


class ModernizationPlans:
    """The Slice port. Same required port surface as ``MaintenanceTasks``:

    * ``execution.submit(record, *, actor)`` -> execution id
      ``execution.status(execution_id)`` -> one of ``EXECUTION_STATES``
      ``execution.events(execution_id, after=0)`` -> ``[{sequence,kind,payload}]``
      ``execution.cost_usd(execution_id)`` -> float
      ``execution.resume(execution_id, *, actor)``
      ``execution.cancel(execution_id, *, actor)``
      ``execution.delivery(execution_id)`` -> ``{commit, checks, unverified,
      working_copy_base_sha, repository, capability_source, synthetic}`` or ``None``
      ``execution.export_artifacts(execution_id)`` -> ``{diff_hash, artifacts}``
    * ``repository.resolve(project_id, repository, base_sha)`` -> identity;
      raises ``ValueError`` on mismatch.
    * ``identity.require(actor, project_id)`` -> ``None``; raises on refusal.
    """

    def __init__(self, store, *, execution, repository, identity):
        self.store = store
        self.records = ModernizationStore(store)
        self.execution = execution
        self.repository = repository
        self.identity = identity

    # -- target / memory ---------------------------------------------------
    def register_target(self, project_id, target, *, actor, source='', paths=(),
                        commit_sha=None) -> list[dict]:
        # ``actor`` here is the resolved identity dict every other port method
        # takes; the memory layer below (``scenario_memory``/``KnowledgeStore``)
        # records a plain username string, same convention as the delivered
        # commit's own ``actor`` field in ``WebuddyExecution.submit``.
        self.identity.require(actor, project_id)
        return register_target(self.store, project_id, target, actor=actor['username'],
                               source=source, paths=paths, commit_sha=commit_sha)

    def confirm_dimension(self, project_id, dimension, *, actor) -> dict:
        self.identity.require(actor, project_id)
        return confirm_dimension(self.store, project_id, dimension, actor=actor['username'])

    def dimensions(self, project_id, *, actor) -> list[dict]:
        self.identity.require(actor, project_id)
        return dimensions_view(self.store, project_id)

    def locate(self, project_id, query, *, actor, limit=10) -> dict:
        self.identity.require(actor, project_id)
        return locate(self.store, project_id, query, limit=limit)

    def disposal_plan(self, project_id, *, actor, queries=None) -> list[dict]:
        self.identity.require(actor, project_id)
        return disposal_plan(self.store, project_id, queries=queries)

    # -- slice creation ------------------------------------------------------
    def create_slice(self, request, *, actor) -> dict:
        normalized = normalize(request)
        self.identity.require(actor, normalized['project_id'])
        self.repository.resolve(normalized['project_id'], normalized['repository'],
                                normalized['base_sha'])
        key = f"modernization:{normalized['project_id']}:{normalized['idempotency_key']}"
        record, created = self.records.claim(key, normalized,
                                            content_fingerprint(normalized))
        self._bind_execution(record, actor=actor)
        return self.get(record['id'], actor=actor)

    def _bind_execution(self, record, *, actor) -> dict:
        """Same recoverable-binding shape as ``MaintenanceTasks._bind_execution``:
        claiming the idempotency key and dispatching are two writes that cannot be
        made one, so a retry finishes an interrupted submit instead of dispatching
        a second execution for the same slice.
        """
        if record.get('execution_id'):
            return record
        execution_id = self.execution.submit(record, actor=actor)
        return self.records.link(record['id'], {'execution_id': execution_id})

    def revise_slice(self, slice_id, request, *, actor) -> dict:
        """A follow-up requirement is a new revision, never an overwrite.

        The predecessor's receipts and the project's confirmed memory both stay
        exactly where they are; the successor reads the same ``dimensions()``
        the predecessor did, because memory is looked up live at submit time,
        not copied into the slice record.
        """
        previous = self.records.get(slice_id)
        self.identity.require(actor, previous['project_id'])
        normalized = normalize(request)
        if normalized['project_id'] != previous['project_id']:
            raise Conflict('新修订必须属于同一个项目')
        fingerprint = content_fingerprint(normalized)
        if fingerprint == previous['content_fingerprint']:
            raise Conflict('新修订与原切片内容完全相同，无需重新受理')
        self.repository.resolve(normalized['project_id'], normalized['repository'],
                                normalized['base_sha'])
        key = f"modernization:{normalized['project_id']}:{normalized['idempotency_key']}"
        record, created = self.records.claim(key, normalized, fingerprint,
                                            predecessor=previous)
        self._bind_execution(record, actor=actor)
        return self.get(record['id'], actor=actor)

    # -- reading --------------------------------------------------------------
    def get(self, slice_id, *, actor) -> dict:
        record = self.records.get(slice_id)
        self.identity.require(actor, record['project_id'])
        return self._view(record)

    def list(self, *, actor, project_id) -> list[dict]:
        self.identity.require(actor, project_id)
        return [self._view(record) for record in
                self.records.slices(project_id=project_id)]

    def _view(self, record) -> dict:
        execution_id = record.get('execution_id')
        status = status_of(self.execution.status(execution_id) if execution_id else None)
        if status in ('running', 'waiting') and record.get('cancel_requested'):
            status = 'cancelling'
        delivery = self.execution.delivery(execution_id) if execution_id else None
        receipts = self.records.receipts(record['id'])
        return {
            'schema_version': SCHEMA_VERSION,
            'slice_id': record['id'], 'revision': record['revision'],
            'predecessor_id': record.get('predecessor_id'),
            'successor_id': record.get('successor_id'),
            'status': status,
            'project_id': record['project_id'],
            'dimension': record['dimension'], 'scope_paths': record['scope_paths'],
            'code_locations': record.get('code_locations', []),
            'baseline': {'repository': record['repository'],
                        'base_sha': record['base_sha'],
                        'base_branch_label': record['base_branch_label']},
            'agreement': record['agreement'],
            'expected_behaviour': record['expected_behaviour'],
            'delivery_goal': record['delivery_goal'],
            'delivery_tier': record['delivery_tier'],
            'known_gaps': record.get('known_gaps', []),
            'synthetic': record.get('synthetic', False),
            'execution_id': execution_id,
            'cost_usd': self.execution.cost_usd(execution_id) if execution_id else 0.0,
            'blocking_reason': self._blocking_reason(status, execution_id),
            'steps': self._steps(status, delivery, receipts),
            'delivery': delivery,
            'receipts': receipts,
            'created_at': record['created_at'],
        }

    BLOCKING_EVENTS = ('run.failed', 'run.blocked', 'run.recovered',
                       'clarification.requested', 'budget.exhausted',
                       'approval.requested', 'requirement_analysis.interrupted')

    #: intake -> locate -> modify -> verify -> deliver -> receipt. A step is
    #: ``done`` only on evidence the execution port reports; there is no timer.
    STEPS = ('intake', 'locate', 'modify', 'verify', 'deliver', 'receipt')

    def _steps(self, status, delivery, receipts=()) -> list[dict]:
        checks = (delivery or {}).get('checks') or []
        reached = {
            'received': 'intake', 'running': 'modify', 'waiting': 'locate',
            'cancelling': 'modify', 'failed': 'modify',
            'cancelled': 'intake', 'delivered': 'receipt',
        }[status]
        if status == 'delivered':
            reached = 'receipt' if (delivery or {}).get('commit') else 'verify'
        if status == 'running' and checks:
            reached = 'verify'
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
        if reached == self.STEPS[-1] and receipts and not halted:
            steps[-1]['state'] = 'done'
        return steps

    def _blocking_reason(self, status, execution_id) -> dict | None:
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
    def events(self, slice_id, *, actor, after=0) -> list[dict]:
        record = self.records.get(slice_id)
        self.identity.require(actor, record['project_id'])
        if not record.get('execution_id'):
            return []
        return self.execution.events(record['execution_id'], after=after)

    def resume(self, slice_id, *, actor) -> dict:
        record = self.records.get(slice_id)
        self.identity.require(actor, record['project_id'])
        if not record.get('execution_id'):
            raise Conflict('这个改造切片还没有绑定执行，无法恢复')
        self.execution.resume(record['execution_id'], actor=actor)
        return self.get(slice_id, actor=actor)

    def cancel(self, slice_id, *, actor) -> dict:
        record = self.records.get(slice_id)
        self.identity.require(actor, record['project_id'])
        self.records.link(slice_id, {'cancel_requested': True})
        if record.get('execution_id'):
            self.execution.cancel(record['execution_id'], actor=actor)
        return self.get(slice_id, actor=actor)

    # -- delivery -----------------------------------------------------------
    def export(self, slice_id, *, actor) -> dict:
        record = self.records.get(slice_id)
        self.identity.require(actor, record['project_id'])
        view = self._view(record)
        if view['status'] != 'delivered':
            raise Conflict(f"改造切片当前状态为 {view['status']}，还没有可交付的结果")
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
        receipt = self.receipt(view, delivery, exported, record)
        stored = self.records.put_receipt(slice_id, record['revision'], receipt)
        return {'receipt': stored, 'text': render_receipt(stored),
                'artifacts': exported['artifacts']}

    @staticmethod
    def receipt(view, delivery, exported, record) -> dict:
        """The machine-readable receipt. A dimension is only "verified" when a
        check whose name plausibly targets it actually ran this round -- absent
        that, or when the caller declared a known gap up front (no 达梦
        instance, no CA SDK, ...), the receipt says so in ``unverified`` rather
        than inheriting a pass from an unrelated check.
        """
        dimension = record['dimension']
        hints = DIMENSION_HINTS.get(dimension, ())
        checks = delivery.get('checks') or []
        ran_names = {str(c.get('name') or '').lower() for c in checks}
        dimension_covered = any(hint in name for hint in hints for name in ran_names)
        unverified = list(delivery.get('unverified') or []) + list(record.get('known_gaps') or [])
        if hints and not dimension_covered:
            unverified.append(
                f"{DIMENSION_LABELS[dimension]}未在真实目标环境验证："
                f"本轮检查未覆盖（需要类似 {'/'.join(hints)} 的检查），不能标记为验证通过")
        return {
            'schema': 'webuddy.legacy_modernization.receipt/1',
            'slice_id': view['slice_id'], 'revision': view['revision'],
            'dimension': dimension, 'baseline': view['baseline'],
            'scope_paths': view['scope_paths'],
            'code_locations': view['code_locations'],
            'delivery': {'commit': delivery.get('commit'),
                         'diff_hash': exported['diff_hash'],
                         'artifacts': [a['name'] for a in exported['artifacts']]},
            'agreement': view['agreement'],
            'checks': [{'name': check.get('name'), 'passed': check.get('passed'),
                        'exit_code': check.get('exit_code'),
                        'reused': bool(check.get('reused')),
                        'identity_fingerprint': check.get('identity_fingerprint')}
                       for check in checks],
            'unverified': unverified,
            'delivery_tier': view['delivery_tier'],
            'capability_source': delivery.get('capability_source') or 'unspecified',
            'synthetic': bool(view.get('synthetic') or delivery.get('synthetic')),
        }


def render_receipt(receipt) -> str:
    lines = [
        f"改造回执 {receipt['slice_id']} 修订 {receipt['revision']}",
        f"维度：{DIMENSION_LABELS.get(receipt['dimension'], receipt['dimension'])}",
        f"范围：{'、'.join(receipt['scope_paths'])}",
        f"基线：{receipt['baseline']['repository']} @ {receipt['baseline']['base_sha']}"
        + (f"（分支标注 {receipt['baseline']['base_branch_label']}）"
           if receipt['baseline']['base_branch_label'] else ''),
        f"交付：commit {receipt['delivery']['commit']}"
        f"，diff {receipt['delivery']['diff_hash']}",
        f"  构建物：{'、'.join(receipt['delivery']['artifacts']) or '（无）'}",
        f"适用约定版本：{receipt['agreement']['revision']}"
        + (f"（Skill {receipt['agreement']['skill_version']}）"
           if receipt['agreement'].get('skill_version') else ''),
    ]
    if receipt['code_locations']:
        lines.append('代码定位：')
        for loc in receipt['code_locations']:
            lines.append(f"  - {loc['path']}"
                         + (f":{loc['line']}" if loc.get('line') else '')
                         + f"（来源：{loc.get('source', 'manual')}）")
    lines.append('检查结果：')
    for check in receipt['checks'] or [{'name': '（无）', 'passed': None, 'exit_code': None}]:
        verdict = '通过' if check['passed'] else '未通过' if check['passed'] is False else '未运行'
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


# ---------------------------------------------------------------------------
# Execution over the existing run store/dispatcher -- same shape as
# ``WebuddyExecution``, own source type and own prompt.
# ---------------------------------------------------------------------------

def modernization_prompt(record, memory: str = '') -> str:
    """The instruction the executor receives.

    Deliberately silent on which of "保行为技术替换" vs "参考另一版本重构" this
    slice is: the task brief requires that decision be made explicitly before
    a slice is drafted, not inferred by the executor from vague wording, so the
    caller states it in ``expected_behaviour``/``delivery_goal`` and this
    function only quotes what was actually decided.
    """
    lines = [
        '按已授权的信创化改造流程处理下面这条范围明确的迁移切片。',
        f"仓库：{record['repository']}",
        f"必须基于基线 commit：{record['base_sha']}",
        f"适用约定版本：{record['agreement']['revision']}",
        f"改造维度：{DIMENSION_LABELS.get(record['dimension'], record['dimension'])}",
        f"范围（只改这些路径，不要扩大范围）：{'、'.join(record['scope_paths'])}",
        f"期望行为：{record['expected_behaviour']}",
        f"交付目标：{record['delivery_goal']}",
    ]
    if record.get('code_locations'):
        lines.append('已知代码定位（含来源，仅作起点，不代表定位已经完整）：')
        for loc in record['code_locations']:
            lines.append(f"  - {loc['path']}"
                         + (f":{loc['line']}" if loc.get('line') else '')
                         + f"（来源：{loc.get('source', 'manual')}）")
    if record.get('known_gaps'):
        lines.append('已知本轮无法打通的验证条件（如实标注，不要假装已验证）：'
                     + '、'.join(record['known_gaps']))
    if memory:
        lines += ['', memory]
    lines += [
        '',
        '先确认这是保行为的技术替换还是参考另一版本的重构；两者不清楚时先问，不要自行假设。',
        '不要修改测试或检查基础设施；缺少的目标环境（如达梦实例、目标 JDK、CA SDK）'
        '如实标记为未验证，不要伪造已经验证通过。',
    ]
    return '\n'.join(lines)


def _unwired(name):
    def refuse(execution_id, actor):
        raise Conflict(f'本进程没有接入{name}，无法对执行 {execution_id} 执行该操作')
    return refuse


class ModernizationExecution:
    """The Execution port over the existing run store and dispatcher."""

    def __init__(self, store, *, dispatch, cost, resume=None, cancel=None, timeout_s=15):
        self.store = store
        self.dispatch = dispatch
        self.cost = cost
        self.resume_hook = resume or _unwired('resume')
        self.cancel_hook = cancel or _unwired('cancel')
        self.timeout_s = timeout_s

    def submit(self, record, *, actor) -> str:
        try:
            memory = scenario_memory.constraints_block(
                scenario_memory.recall(self.store, record['project_id']))
        except (KeyError, ValueError):
            memory = '（项目记忆暂时读不到，本次没有携带已确认约束）'
        run, created = self.store.create_run(
            record['project_id'], modernization_prompt(record, memory),
            source={'type': SOURCE_TYPE, 'actor': actor['username'],
                    'actor_id': actor['id'],
                    'modernization_slice_id': record['id'],
                    'modernization_revision': record['revision'],
                    'dimension': record['dimension'],
                    'agreement_revision': record['agreement']['revision'],
                    'request_fingerprint': record['content_fingerprint'],
                    'repository': record['repository'],
                    'expected_base_sha': record['base_sha'],
                    'delivery_tier': record['delivery_tier'],
                    'synthetic': record.get('synthetic', False)},
            delivery_id=f"modernization:{record['id']}",
            semantic_id=f"modernization:{record['project_id']}:{record['content_fingerprint']}")
        if created or not self._dispatched(run):
            self.dispatch(run['id'])
        return run['id']

    def _dispatched(self, run) -> bool:
        if run.get('status') != 'received':
            return True
        from factory.control.autonomy import DurableQueue
        return bool(DurableQueue(self.store).jobs(run['id']))

    def _run(self, execution_id) -> dict:
        run = self.store.get(execution_id)
        if (run.get('source') or {}).get('type') != SOURCE_TYPE:
            raise Conflict('这个执行不属于信创化改造模块，拒绝当作改造证据读取')
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

    def resume(self, execution_id, *, actor):
        run = self._run(execution_id)
        if run['status'] != 'needs_human':
            raise Conflict(f"执行当前状态为 {run['status']}，不需要恢复")
        return self.resume_hook(execution_id, actor)

    def cancel(self, execution_id, *, actor):
        return self.cancel_hook(execution_id, actor)

    def delivery(self, execution_id) -> dict | None:
        run = self._run(execution_id)
        artifacts = run.get('artifacts') or {}
        if not artifacts.get('commit'):
            return None
        source = run.get('source') or {}
        return {
            'commit': artifacts['commit'],
            'checks': [{'name': check.get('name'), 'passed': check.get('exit') == 0,
                        'exit_code': check.get('exit'),
                        'reused': bool(check.get('reused')),
                        'identity_fingerprint': check.get('identity_fingerprint')}
                       for check in (artifacts.get('checks') or [])],
            'unverified': list(artifacts.get('unverified') or []),
            'working_copy_base_sha': self._working_copy_base_sha(artifacts),
            'repository': source.get('repository'),
            'capability_source': source.get('capability_source') or 'platform_executor',
            'synthetic': bool(source.get('synthetic')),
            'worktree': artifacts.get('worktree'),
        }

    def _working_copy_base_sha(self, artifacts) -> str | None:
        worktree, commit = artifacts.get('worktree'), artifacts.get('commit')
        base = artifacts.get('base_sha')
        if not worktree or not commit or not base or not Path(worktree).is_dir():
            return None
        done = subprocess.run(
            ['git', 'merge-base', '--is-ancestor', base, commit], cwd=worktree,
            capture_output=True, timeout=self.timeout_s)
        return base if done.returncode == 0 else None

    def export_artifacts(self, execution_id) -> dict:
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
                'artifacts': [{'name': f'modernization-{execution_id}.patch',
                               'bytes': done.stdout}]}


#: Task states that mean work is still live, as the existing engine counts it.
ACTIVE_SLICE_STATES = frozenset({'received', 'running', 'waiting', 'cancelling'})


def active_slice_count(store) -> int:
    """How many modernization slices are still live, across every project.

    Same "unknown counts as live" rule as ``issue_maintenance_webuddy.
    active_task_count``, for the same reason: this answers a question that
    decides whether an administrator's stop is allowed, and the safe direction
    for an unreadable state is to refuse the stop, not to grant it.
    """
    live = 0
    for record in ModernizationStore(store).slices():
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
        live += status in ACTIVE_SLICE_STATES
    return live


def availability_for(store):
    from factory.control.plugins import PluginAvailability
    return PluginAvailability(store)


#: Which availability class each business-port method falls into, mirroring
#: ``plugins.MAINTENANCE_ACTIONS``. A method absent here is not reachable
#: through the gated port at all.
ACTIONS = {
    'register_target': 'create',
    'confirm_dimension': 'create',
    'create_slice': 'create',
    'revise_slice': 'create',
    'resume': 'continue',
    'get': 'always',
    'list': 'always',
    'dimensions': 'always',
    'locate': 'always',
    'disposal_plan': 'always',
    'events': 'always',
    'cancel': 'always',
    'export': 'always',
}


def plans_for(svc, *, identity=None, dispatch=None, availability=None):
    """Assemble the Slice port from a live service. One wiring, web and CLI alike.

    Same shape as ``issue_maintenance_webuddy.tasks_for``: the return value is
    always gated, so there is no ungated form for a surface to reach instead.
    """
    from factory.control.plugins import gated
    store = svc.store
    port = ModernizationPlans(
        store,
        execution=ModernizationExecution(
            store, dispatch=svc.start_plan if dispatch is None else dispatch,
            cost=lambda eid: svc._usage(eid)['known_cost_usd'],
            resume=lambda eid, actor: svc.continue_run(
                eid, '', store.get(eid)['revision'],
                store.get(eid).get('resume_count', 0), actor['username']),
            cancel=lambda eid, actor: svc.cancel(eid, actor['username'])),
        repository=WebuddyRepository(store),
        identity=WebuddyIdentity(svc.governance) if identity is None else identity)
    return gated(port,
                 availability if availability is not None else availability_for(store),
                 PLUGIN_ID, ACTIONS)
