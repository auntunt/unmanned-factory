"""Durable run state and append-only, redacted engineering events."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from factory.redact import redact_text

ACTIVE = ('received', 'planning', 'queued', 'running', 'verifying', 'publishing')
SECRET_KEY = re.compile(r'(?i)^(password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|token|csrf_token|credential|private_key)$')


def now():
    return datetime.now(timezone.utc).isoformat()


def scrub(value):
    if isinstance(value, dict):
        return {str(k): '***REDACTED***' if SECRET_KEY.match(str(k)) else scrub(v)
                for k, v in value.items() if str(k) not in {'thinking', 'reasoning', 'chain_of_thought'}}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        text = redact_text(value)
        for key, secret in os.environ.items():
            if len(secret) >= 8 and re.search(r'(?i)(TOKEN|PASSWORD|SECRET|API_KEY)$', key):
                text = text.replace(secret, '***REDACTED***')
        return text if len(text) <= 100_000 else text[:100_000] + '\n[输出已截断：超过 100000 字符]'
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
                CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,run_id TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_event_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_event_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
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
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM projects ORDER BY rowid DESC')]

    def project(self, pid):
        with self.connect() as db:
            row = db.execute('SELECT data FROM projects WHERE id=?', (pid,)).fetchone()
            if row is None:
                raise KeyError(pid)
            return json.loads(row[0])

    def add_project(self, data):
        project = {**data, 'id': uuid.uuid4().hex, 'created_at': now()}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # One registered project per repository gives webhook an unambiguous target.
            for row in db.execute('SELECT data FROM projects'):
                if json.loads(row[0])['repository'].casefold() == project['repository'].casefold():
                    raise Conflict('该仓库已登记')
            db.execute('INSERT INTO projects VALUES (?,?)', (project['id'], json.dumps(project)))
        return project

    @staticmethod
    def _event(db, rid, kind, payload, task_id=None):
        return db.execute('INSERT INTO events(run_id,task_id,type,payload,at) VALUES (?,?,?,?,?)',
                          (rid, task_id, kind, json.dumps(scrub(payload), ensure_ascii=False), now())).lastrowid

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
            return json.loads(row[0])

    def runs(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM runs ORDER BY rowid DESC LIMIT 200')]

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
