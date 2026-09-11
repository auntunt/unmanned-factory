"""Durable run state and append-only, redacted engineering events."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from factory.redact import redact_text

ACTIVE = ('received', 'planning', 'queued', 'running', 'verifying', 'publishing')
PROJECT_EDIT_BLOCKING = frozenset((*ACTIVE, 'awaiting_approval', 'needs_clarification', 'ready_for_review'))
PROJECT_BUDGET_INCREASE_BLOCKING = PROJECT_EDIT_BLOCKING - {'ready_for_review'}
PROJECT_BUDGET_DECREASE_BLOCKING = PROJECT_EDIT_BLOCKING | {'needs_human'}
SECRET_KEY = re.compile(r'(?i)^(password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|token|csrf_token|credential|private_key)$')


def now():
    return datetime.now(timezone.utc).isoformat()


def scrub(value, *, max_chars=100_000):
    if isinstance(value, dict):
        return {str(k): '***REDACTED***' if SECRET_KEY.match(str(k)) else scrub(v, max_chars=max_chars)
                for k, v in value.items() if str(k) not in {'thinking', 'reasoning', 'chain_of_thought'}}
    if isinstance(value, (list, tuple)):
        return [scrub(v, max_chars=max_chars) for v in value]
    if isinstance(value, str):
        text = redact_text(value)
        for key, secret in os.environ.items():
            if len(secret) >= 8 and re.search(r'(?i)(TOKEN|PASSWORD|SECRET|API_KEY)$', key):
                text = text.replace(secret, '***REDACTED***')
        return text if max_chars is None or len(text) <= max_chars else text[:max_chars] + f'\n[输出已截断：超过 {max_chars} 字符]'
    return value


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, task_id TEXT, type TEXT NOT NULL,
                    payload TEXT NOT NULL, at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS event_archives(
                    event_id INTEGER PRIMARY KEY, content BLOB NOT NULL,
                    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_archive_update BEFORE UPDATE ON event_archives
                    BEGIN SELECT RAISE(ABORT,'event archives are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_archive_delete BEFORE DELETE ON event_archives
                    BEGIN SELECT RAISE(ABORT,'event archives are immutable'); END;
                CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,run_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_settings_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    data TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS no_event_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_event_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_project_audit_update BEFORE UPDATE ON project_settings_audit
                    BEGIN SELECT RAISE(ABORT,'project settings audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_project_audit_delete BEFORE DELETE ON project_settings_audit
                    BEGIN SELECT RAISE(ABORT,'project settings audit is append-only'); END;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def projects(self):
        with self.connect() as db:
            return [self._project_view(json.loads(r[0])) for r in db.execute('SELECT data FROM projects ORDER BY rowid DESC')]

    def project(self, pid):
        with self.connect() as db:
            row = db.execute('SELECT data FROM projects WHERE id=?', (pid,)).fetchone()
            if row is None:
                raise KeyError(pid)
            return self._project_view(json.loads(row[0]))

    @staticmethod
    def _project_view(data):
        """Expose legacy rows with revision 1 without rewriting their JSON."""
        return {**data, 'revision': int(data.get('revision', 1))}

    def add_project(self, data):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._insert_project(db, data)

    def _insert_project(self, db, data):
        """Insert inside a caller-owned transaction (also used by workspace provisioning)."""
        project = {**data, 'id': uuid.uuid4().hex, 'revision': 1, 'created_at': now()}
        for row in db.execute('SELECT data FROM projects'):
            if json.loads(row[0])['repository'].casefold() == project['repository'].casefold():
                raise Conflict('该仓库已登记')
        db.execute('INSERT INTO projects VALUES (?,?)', (project['id'], json.dumps(project)))
        db.execute('INSERT INTO project_settings_audit(project_id,revision,actor,action,data,at) VALUES (?,?,?,?,?,?)',
                   (project['id'], 1, str(data.get('actor', 'system')), 'created',
                    json.dumps(project, ensure_ascii=False), project['created_at']))
        return project

    def update_project(self, pid, changes, expected_revision, actor):
        """CAS update of mutable project settings with an append-only audit row."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('项目 revision 必须是正整数')
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError('项目设置修改人不能为空')
        allowed = {'name', 'base_branch', 'checks', 'auto_issues', 'auto_publish', 'budget_usd'}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError('项目设置包含不可修改字段')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM projects WHERE id=?', (pid,)).fetchone()
            if row is None:
                raise KeyError(pid)
            project = json.loads(row[0])
            current = int(project.get('revision', 1))
            if current != expected_revision:
                raise Conflict('项目设置已更新，请重新加载后再保存')
            effective = {key: value for key, value in changes.items()
                         if project.get(key) != value}
            old_budget, new_budget = project.get('budget_usd'), effective.get('budget_usd')
            numeric_budget_change = (
                'budget_usd' in effective
                and type(old_budget) in (int, float)
                and type(new_budget) in (int, float)
                and math.isfinite(float(old_budget))
                and math.isfinite(float(new_budget)))
            if numeric_budget_change and new_budget < old_budget:
                # A paused run may be relying on its current ceiling. Apply this
                # protection even when the request edits other settings too.
                blocking_statuses = PROJECT_BUDGET_DECREASE_BLOCKING
            elif (set(effective) == {'budget_usd'} and numeric_budget_change
                    and new_budget > old_budget):
                blocking_statuses = PROJECT_BUDGET_INCREASE_BLOCKING
            else:
                blocking_statuses = PROJECT_EDIT_BLOCKING
            busy = db.execute("SELECT 1 FROM runs WHERE json_extract(data, '$.project_id')=? "
                              "AND json_extract(data, '$.status') IN (%s) LIMIT 1" %
                              ','.join('?' for _ in blocking_statuses),
                              (pid, *sorted(blocking_statuses))).fetchone()
            if busy is not None:
                raise Conflict('项目存在进行中的运行，暂时不能修改设置')
            updated = {**project, **changes, 'revision': current + 1, 'updated_at': now()}
            db.execute('UPDATE projects SET data=? WHERE id=?',
                       (json.dumps(updated, ensure_ascii=False), pid))
            db.execute('INSERT INTO project_settings_audit(project_id,revision,actor,action,data,at) VALUES (?,?,?,?,?,?)',
                       (pid, updated['revision'], actor.strip(), 'updated',
                        json.dumps(changes, ensure_ascii=False), updated['updated_at']))
        return self._project_view(updated)

    def project_audit(self, pid):
        self.project(pid)
        with self.connect() as db:
            rows = db.execute('SELECT id,project_id,revision,actor,action,data,at '
                              'FROM project_settings_audit WHERE project_id=? ORDER BY id', (pid,)).fetchall()
        return [{**dict(row), 'data': json.loads(row['data'])} for row in rows]

    @staticmethod
    def _event(db, rid, kind, payload, task_id=None):
        summary = json.dumps(scrub(payload), ensure_ascii=False)
        eid = db.execute('INSERT INTO events(run_id,task_id,type,payload,at) VALUES (?,?,?,?,?)',
                          (rid, task_id, kind, summary, now())).lastrowid
        full = json.dumps(scrub(payload, max_chars=None), ensure_ascii=False)
        if full != summary:
            raw = full.encode('utf-8')
            db.execute('INSERT INTO event_archives VALUES (?,?,?,?)',
                       (eid, zlib.compress(raw), hashlib.sha256(raw).hexdigest(), len(raw)))
        return eid

    def create_run(self, project_id, request, *, source=None, delivery_id=None, semantic_id=None):
        rid = uuid.uuid4().hex
        data = dict(id=rid, project_id=project_id, request=scrub(request), status='received',
                    revision=0, plan=None, triage=None, tasks=[], artifacts={},
                    history=[], source=source or {'type': 'web'}, created_at=now(), updated_at=now())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            keys = [k for k in (delivery_id, 'semantic:' + semantic_id if semantic_id else None) if k]
            for key in keys:
                row = db.execute('SELECT run_id FROM deliveries WHERE id=?', (key,)).fetchone()
                if row:
                    for other in keys:
                        db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?)', (other, row[0]))
                    return self.get(row[0]), False
            for key in keys:
                db.execute('INSERT INTO deliveries VALUES (?,?)', (key, rid))
            if data['source'].get('type') == 'github':
                for row in db.execute('SELECT data FROM runs ORDER BY rowid DESC'):
                    prior = json.loads(row[0])
                    if (prior['project_id'] == project_id and
                            prior['source'].get('type') == 'github' and
                            prior['source'].get('issue_number') == data['source'].get('issue_number')):
                        data['source']['previous_run_id'] = prior['id']
                        break
            db.execute('INSERT INTO runs VALUES (?,?)', (rid, json.dumps(data, ensure_ascii=False)))
            self._event(db, rid, 'user.message', {'text': data['request'], 'source': data['source']})
        return data, True

    def get(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT data FROM runs WHERE id=?', (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            return self._run_view(db, json.loads(row[0]))

    @staticmethod
    def _run_view(db, run):
        # Older workers recorded the stopping error only in the event stream.
        if run.get("status") == "needs_human":
            event = db.execute("SELECT type,payload FROM events WHERE run_id=? AND type IN ('run.failed','run.started','run.resumed','run.recovered','run.planning','run.verified') ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
            if event and event[0] == "run.failed":
                run["error"] = json.loads(event[1]).get("message")
        return run

    def runs(self):
        with self.connect() as db:
            return [self._run_view(db, json.loads(r[0])) for r in db.execute('SELECT data FROM runs ORDER BY rowid DESC LIMIT 200')]

    def all_runs(self):
        """Accounting and recovery must not silently omit older runs."""
        with self.connect() as db:
            return [self._run_view(db, json.loads(r[0])) for r in db.execute('SELECT data FROM runs ORDER BY rowid DESC')]

    def published_runs_for_pr(self, project_id, number, repository):
        """Find deliveries independently of the dashboard's recent-run limit."""
        self.project(project_id)
        canonical = f'https://github.com/{repository}/pull/{number}'.casefold()
        with self.connect() as db:
            rows = db.execute("SELECT data FROM runs WHERE json_extract(data, '$.project_id')=? "
                              "AND json_extract(data, '$.status')='published'", (project_id,))
            result = []
            for row in rows:
                run = json.loads(row[0])
                artifacts = run.get('artifacts', {})
                if (artifacts.get('pr_number') == number or
                        str(artifacts.get('pr_url', '')).casefold() == canonical):
                    result.append(run)
            return result

    def update(self, rid, changes, *, expected=None, revision=None, event=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM runs WHERE id=?', (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            data = json.loads(row[0])
            if expected is not None and data['status'] not in expected:
                raise Conflict(f"当前状态 {data['status']} 不允许该操作")
            if revision is not None and data['revision'] != revision:
                raise Conflict('计划已更新，请重新查看并确认当前版本')
            data.update(scrub(changes))
            data['updated_at'] = now()
            db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), rid))
            if event:
                self._event(db, rid, event[0], event[1])
            return data

    def append(self, rid, kind, payload, task_id=None):
        with self.connect() as db:
            return self._event(db, rid, kind, payload, task_id)

    def events(self, rid, after=0, limit=500):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?',
                              (rid, after, min(limit, 2000))).fetchall()
            return [{**dict(r), 'payload': json.loads(r['payload']), 'version': 1} for r in rows]

    def export_events(self, rid, *, through=None):
        """Read the complete stored payload against a stable event watermark."""
        with self.connect() as db:
            rows = db.execute('''SELECT e.*, a.content AS archive, a.sha256 AS archive_sha
                FROM events e LEFT JOIN event_archives a ON a.event_id=e.id
                WHERE e.run_id=? AND (? IS NULL OR e.id<=?) ORDER BY e.id''',
                (rid, through, through))
            for row in rows:
                value = dict(row)
                archive, sha = value.pop('archive'), value.pop('archive_sha')
                if archive is not None:
                    raw = zlib.decompress(archive)
                    if hashlib.sha256(raw).hexdigest() != sha:
                        raise ValueError('事件归档完整性校验失败')
                    value['payload'] = json.loads(raw)
                    value['archive_sha256'] = sha
                else:
                    value['payload'] = json.loads(value['payload'])
                value['version'] = 1
                yield value

    def conversation(self, rid):
        messages, after = [], 0
        while True:
            events = self.events(rid, after, 2000)
            if not events:
                break
            for e in events:
                p = e['payload']
                role = 'user' if e['type'] == 'user.message' else 'assistant'
                content = None
                if e['type'] in ('user.message', 'assistant.message'):
                    content = p.get('text') or p.get('content')
                elif e['type'] == 'plan.created':
                    content = p.get('summary', '')
                elif e['type'] == 'triage.decided':
                    content = '\n'.join(p.get('reasons', []) + p.get('questions', []))
                elif e['type'] in ('run.failed', 'run.recovered', 'run.cancelled', 'github.published'):
                    content = p.get('message') or p.get('pr_url')
                if content:
                    messages.append(dict(id=e['id'], role=role, content=content,
                                         event_ids=[e['id']], at=e['at'], task_id=e['task_id']))
            after = events[-1]['id']
        return messages

    def recover(self):
        with self.connect() as db:
            all_runs = [json.loads(row[0]) for row in db.execute('SELECT data FROM runs')]
        for data in all_runs:
            if data['status'] in ACTIVE:
                self.update(data['id'], {'status': 'needs_human'}, expected=ACTIVE,
                            event=('run.recovered', {'message': '服务重启；上次执行结果待核对，保留现场，不自动重跑。'}))
