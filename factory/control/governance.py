"""Trusted-team access and a durable token dispatch ledger.

Reservations gate SDK *tasks*, not each hidden model turn. Actual task usage may
exceed a reservation. Missing usage is retained for reconciliation, never zeroed.
The ledger covers this factory only, not a provider account's other clients.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from factory.control.auth import AuthError
from factory.control.providers import ProviderError, ProviderCancelled
from factory.control.store import now, scrub


def month_now():
    return datetime.now(timezone.utc).strftime('%Y-%m')


def token_count(value):
    return value if type(value) is int and value >= 0 else None


class QuotaExceeded(ProviderError):
    pass


class Governance:
    def __init__(self, auth, store):
        self.auth, self.store = auth, store
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS team_projects(
                    user_id INTEGER NOT NULL REFERENCES users(id), project_id TEXT NOT NULL,
                    PRIMARY KEY(user_id,project_id));
                CREATE TABLE IF NOT EXISTS token_limits(
                    scope TEXT NOT NULL, scope_id TEXT NOT NULL, limit_tokens INTEGER,
                    PRIMARY KEY(scope,scope_id));
                CREATE TABLE IF NOT EXISTS team_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1), reservation_tokens INTEGER NOT NULL);
                INSERT OR IGNORE INTO team_settings VALUES(1,20000);
                CREATE TABLE IF NOT EXISTS team_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS token_calls(
                    id TEXT PRIMARY KEY,run_id TEXT,actor_id INTEGER,project_id TEXT,
                    provider TEXT NOT NULL,model TEXT NOT NULL,month TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL,actual_tokens INTEGER,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,settled_at TEXT);
                CREATE INDEX IF NOT EXISTS token_calls_month ON token_calls(month,actor_id,project_id);
                CREATE TABLE IF NOT EXISTS team_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,
                    action TEXT NOT NULL,data TEXT NOT NULL,at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_team_audit_update BEFORE UPDATE ON team_audit
                    BEGIN SELECT RAISE(ABORT,'team audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_team_audit_delete BEFORE DELETE ON team_audit
                    BEGIN SELECT RAISE(ABORT,'team audit is append-only'); END;
            ''')
            db.execute("INSERT OR IGNORE INTO team_meta VALUES('tracking_started_at',?)", (now(),))

    @contextmanager
    def connect(self):
        with self.auth._connection() as db:
            db.row_factory = sqlite3.Row
            yield db

    @staticmethod
    def _audit(db, actor, action, data):
        db.execute('INSERT INTO team_audit(actor,action,data,at) VALUES(?,?,?,?)',
                   (str(actor), action, json.dumps(scrub(data), ensure_ascii=False), now()))

    def audit(self, actor, action, data):
        with self.connect() as db:
            self._audit(db, actor, action, data)

    @staticmethod
    def _can_run(db, user_id, project_id):
        user = db.execute('SELECT role,active FROM users WHERE id=?', (user_id,)).fetchone()
        return bool(user and user['active'] and (user['role'] == 'admin' or db.execute(
            'SELECT 1 FROM team_projects WHERE user_id=? AND project_id=?', (user_id, project_id)).fetchone()))

    def require_project(self, user_id, project_id):
        self.store.project(project_id)
        with self.connect() as db:
            if not self._can_run(db, user_id, project_id):
                raise AuthError('你没有该项目的执行权限，请联系管理员分配项目', 403)

    def assign(self, user_id, project_ids, actor):
        for pid in project_ids:
            self.store.project(pid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                raise AuthError('成员不存在', 404)
            db.execute('DELETE FROM team_projects WHERE user_id=?', (user_id,))
            db.executemany('INSERT INTO team_projects VALUES(?,?)', [(user_id, pid) for pid in sorted(set(project_ids))])
            self._audit(db, actor, 'member.projects', {'user_id': user_id, 'project_ids': sorted(set(project_ids))})

    def set_limit(self, scope, scope_id, limit_tokens, actor):
        if scope not in ('member', 'project', 'workspace') or (limit_tokens is not None and token_count(limit_tokens) is None):
            raise AuthError('额度设置无效', 422)
        scope_id = str(scope_id)
        if scope == 'project':
            self.store.project(scope_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if scope == 'workspace' and scope_id != 'all':
                raise AuthError('工作区额度范围无效', 422)
            if scope == 'member':
                if not scope_id.isdigit() or str(int(scope_id)) != scope_id or not db.execute('SELECT 1 FROM users WHERE id=?', (scope_id,)).fetchone():
                    raise AuthError('成员不存在', 404)
            db.execute('INSERT INTO token_limits VALUES(?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET limit_tokens=excluded.limit_tokens',
                       (scope, scope_id, limit_tokens))
            self._audit(db, actor, 'quota.updated', {'scope': scope, 'scope_id': scope_id, 'limit_tokens': limit_tokens})

    def set_reservation(self, tokens, actor):
        if token_count(tokens) is None or not 1 <= tokens <= 1_000_000:
            raise AuthError('单次预留必须在 1 到 1,000,000 tokens 之间', 422)
        with self.connect() as db:
            db.execute('UPDATE team_settings SET reservation_tokens=? WHERE id=1', (tokens,))
            self._audit(db, actor, 'reservation.updated', {'reservation_tokens': tokens})

    @staticmethod
    def _quota(db, scope, scope_id, month):
        row = db.execute('SELECT limit_tokens FROM token_limits WHERE scope=? AND scope_id=?', (scope, str(scope_id))).fetchone()
        limit = row[0] if row else None
        clause, args = ('', []) if scope == 'workspace' else (
            ' AND actor_id=?', [scope_id]) if scope == 'member' else (' AND project_id=?', [scope_id])
        # Outstanding calls survive a calendar boundary; known usage stays in
        # its dispatch month. Otherwise a restart on day 1 could erase debt.
        rows = db.execute("SELECT status,reserved_tokens,actual_tokens,month FROM token_calls WHERE (month=? OR status IN ('reserved','unknown'))" + clause,
                          [month, *args]).fetchall()
        used = sum(row['actual_tokens'] or 0 for row in rows if row['status'] == 'settled' and row['month'] == month)
        reserved = sum(row['reserved_tokens'] for row in rows if row['status'] in ('reserved', 'unknown'))
        unknown = sum(row['status'] == 'unknown' for row in rows)
        return {'limit_tokens': limit, 'used_tokens': used, 'reserved_tokens': reserved,
                'unknown_calls': unknown, 'remaining_tokens': None if limit is None else max(0, limit - used - reserved)}

    def reserve(self, run_id, provider, model, *, actor_id=None):
        project_id = None
        if run_id:
            run = self.store.get(run_id)
            project_id = run['project_id']
            actor_id = run.get('source', {}).get('actor_id')
        if actor_id is not None and (type(actor_id) is not int or actor_id < 1):
            raise QuotaExceeded('运行的成员归属无效，无法分配额度')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if actor_id is not None:
                if project_id and not self._can_run(db, actor_id, project_id):
                    raise QuotaExceeded('成员已停用或项目执行权限已撤回')
                if not project_id and not db.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role='admin'", (actor_id,)).fetchone():
                    raise QuotaExceeded('仅启用的管理员可以发起运行探针')
            reserve = db.execute('SELECT reservation_tokens FROM team_settings WHERE id=1').fetchone()[0]
            month = month_now()
            scopes = [('workspace', 'all')]
            if project_id:
                scopes.append(('project', project_id))
            if actor_id is not None:
                scopes.append(('member', str(actor_id)))
            for scope, sid in scopes:
                quota = self._quota(db, scope, sid, month)
                label = {'workspace': '工作区', 'project': '项目', 'member': '成员'}[scope]
                if quota['limit_tokens'] is not None:
                    if quota['unknown_calls']:
                        raise QuotaExceeded(f'{label}有待核对的模型用量，核对后才能继续调用')
                    if quota['remaining_tokens'] < reserve:
                        raise QuotaExceeded(f'{label}额度不足：下次模型任务需预留 {reserve:,} tokens，当前可用 {quota["remaining_tokens"]:,}')
            call = dict(id=uuid.uuid4().hex, run_id=run_id, actor_id=actor_id, project_id=project_id,
                        provider=provider, model=model, month=month, reserved_tokens=reserve,
                        actual_tokens=None, status='reserved', created_at=now(), settled_at=None)
            db.execute('INSERT INTO token_calls VALUES(:id,:run_id,:actor_id,:project_id,:provider,:model,:month,:reserved_tokens,:actual_tokens,:status,:created_at,:settled_at)', call)
            return call

    def settle(self, call_id, actual_tokens):
        actual_tokens = token_count(actual_tokens)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE token_calls SET actual_tokens=?,status=?,settled_at=? WHERE id=? AND status='reserved'",
                       (actual_tokens, 'settled' if actual_tokens is not None else 'unknown', now(), call_id))
            row = db.execute('SELECT * FROM token_calls WHERE id=?', (call_id,)).fetchone()
            if row is None:
                raise KeyError(call_id)
            return dict(row)

    def recover(self):
        # Invoke only after acquiring the service's single-coordinator lock.
        with self.connect() as db:
            db.execute("UPDATE token_calls SET status='unknown',settled_at=? WHERE status='reserved'", (now(),))

    def reconcile(self, call_id, actual_tokens, reason, actor):
        if token_count(actual_tokens) is None or not isinstance(reason, str) or len(reason.strip()) < 3:
            raise AuthError('请提供核对后的 token 数量和依据', 422)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM token_calls WHERE id=?', (call_id,)).fetchone()
            if row is None:
                raise AuthError('调用记录不存在', 404)
            if row['status'] != 'unknown':
                raise AuthError('仅待核对用量可以人工结算', 409)
            db.execute("UPDATE token_calls SET status='settled',actual_tokens=?,settled_at=? WHERE id=?", (actual_tokens, now(), call_id))
            self._audit(db, actor, 'usage.reconciled', {'call_id': call_id, 'actual_tokens': actual_tokens, 'reason': reason.strip()})
            return {**dict(row), 'status': 'settled', 'actual_tokens': actual_tokens}

    def summary(self, user):
        admin, month = user['role'] == 'admin', month_now()
        projects = self.store.projects()
        with self.connect() as db:
            members = [dict(row) for row in db.execute('SELECT id,username,role,active FROM users ORDER BY id') if admin or row['id'] == user['id']]
            assignments = [dict(row) for row in db.execute('SELECT * FROM team_projects')]
            for member in members:
                member['active'] = bool(member['active'])
                member['project_ids'] = [p['id'] for p in projects] if member['role'] == 'admin' else [a['project_id'] for a in assignments if a['user_id'] == member['id']]
                member['quota'] = self._quota(db, 'member', member['id'], month)
            project_views = [{'id': p['id'], 'name': p['name'],
                'member_ids': [a['user_id'] for a in assignments if a['project_id'] == p['id'] and (admin or a['user_id'] == user['id'])],
                'quota': self._quota(db, 'project', p['id'], month)} for p in projects]
            clause, args = ('', []) if admin else (' WHERE actor_id=?', [user['id']])
            calls = [dict(row) for row in db.execute('SELECT * FROM token_calls' + clause + " ORDER BY CASE WHEN status='unknown' THEN 0 WHEN status='reserved' THEN 1 ELSE 2 END,created_at DESC LIMIT 100", args)]
            audit = [{**dict(row), 'data': json.loads(row['data'])} for row in db.execute('SELECT * FROM team_audit ORDER BY id DESC LIMIT 50')] if admin else []
            return {'month': month, 'timezone': 'UTC', 'reservation_tokens': db.execute('SELECT reservation_tokens FROM team_settings WHERE id=1').fetchone()[0],
                    'tracking_started_at': db.execute("SELECT value FROM team_meta WHERE key='tracking_started_at'").fetchone()[0],
                    'workspace': self._quota(db, 'workspace', 'all', month), 'members': members,
                    'projects': project_views, 'calls': calls, 'audit': audit}


class GovernedRunner:
    def __init__(self, runner, governance, run_id=None, *, actor_id=None):
        self.runner, self.governance, self.run_id, self.actor_id = runner, governance, run_id, actor_id

    def available(self):
        return self.runner.available()

    def run(self, request, emit, cancel=None):
        if cancel is not None and cancel.is_set():
            raise ProviderCancelled('execution cancelled before dispatch')
        call = self.governance.reserve(self.run_id, request.provider, request.model, actor_id=self.actor_id)
        result = None
        try:
            emit('quota.reserved', {key: call[key] for key in ('id', 'actor_id', 'reserved_tokens', 'month')})
            result = self.runner.run(request, emit, cancel=cancel)
            return result
        finally:
            # Partial streaming usage may omit the final billable turn. It is
            # evidence for an administrator, not proof of complete settlement.
            incoming = token_count(getattr(result, 'tokens_in', None))
            outgoing = token_count(getattr(result, 'tokens_out', None))
            actual = incoming + outgoing if incoming is not None and outgoing is not None else None
            settled = self.governance.settle(call['id'], actual)
            emit('quota.settled', {key: settled[key] for key in ('id', 'actor_id', 'actual_tokens', 'reserved_tokens', 'status')})
