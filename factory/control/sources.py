"""Project-owned, immutable reference collections. No executable configuration.

Operators acquire data outside the coding worker. Modules reference a reviewed
collection revision; runs pin its body. A current enabled flag permits revoking
future dispatches without rewriting historical evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from factory.control.store import Conflict, now, scrub

MAX_BYTES = 200_000


def source_slots(value):
    if (not isinstance(value, list) or len(value) > 12
            or any(not isinstance(v, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', v) for v in value)
            or len(set(value)) != len(value)):
        raise ValueError('数据源槽位必须是最多 12 个不重复的小写英文标识')
    return list(value)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def source_refs(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError('最多挂载 12 个数据源版本')
    result, seen = [], set()
    for ref in value:
        if (not isinstance(ref, dict) or set(ref) != {'id', 'revision'}
                or not isinstance(ref['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', ref['id'])
                or type(ref['revision']) is not int or ref['revision'] < 1):
            raise ValueError('数据源引用必须包含有效 id 和 revision')
        if ref['id'] in seen:
            raise ValueError('同一模块不能重复引用数据源')
        seen.add(ref['id']); result.append(dict(ref))
    return result


def normalize_documents(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError('数据源必须包含 1 到 100 篇文档')
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {'id', 'title', 'text', 'uri'}:
            raise ValueError('文档仅支持 id、title、text、uri')
        doc = {}
        for name, limit in [('id', 128), ('title', 200), ('text', 40_000), ('uri', 1000)]:
            raw = item.get(name, '')
            if not isinstance(raw, str) or len(raw) > limit or '\x00' in raw or (name != 'uri' and not raw.strip()):
                raise ValueError(f'文档 {name} 为空或超出限额')
            doc[name] = scrub(raw)
        if doc['id'] in seen:
            raise ValueError('文档 id 重复')
        seen.add(doc['id'])
        doc['sha256'] = hashlib.sha256(doc['text'].encode()).hexdigest()
        result.append(doc)
    if len(encoded(result).encode()) > MAX_BYTES:
        raise ValueError('单个数据源超出 200 KB；请按业务主题拆分')
    return result


class SourceStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS data_sources(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS data_source_versions(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS data_source_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_source_slots(
                    project_id TEXT NOT NULL, slot TEXT NOT NULL, revision INTEGER NOT NULL,
                    source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                    PRIMARY KEY(project_id,slot));
                CREATE TRIGGER IF NOT EXISTS no_source_version_update BEFORE UPDATE ON data_source_versions
                    BEGIN SELECT RAISE(ABORT,'source versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_source_version_delete BEFORE DELETE ON data_source_versions
                    BEGIN SELECT RAISE(ABORT,'source versions are immutable'); END;
            ''')

    def put(self, pid, *, name, documents, actor, sid=None, expected_revision=0, transport='json'):
        self.store.project(pid)
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError('数据源名称无效')
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            raise ValueError('必须记录数据源配置人')
        if transport not in ('json', 'cli', 'mcp-resource'):
            raise ValueError('不支持的数据源传输方式')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected_revision 无效')
        docs = normalize_documents(documents)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM data_sources WHERE id=?', (sid,)).fetchone() if sid else None
            if sid and (not row or row['project_id'] != pid):
                raise ValueError('数据源不属于该项目')
            if (row['revision'] if row else 0) != expected_revision:
                raise Conflict('数据源版本已变化，请重新读取')
            sid = sid or uuid.uuid4().hex
            revision = expected_revision + 1
            body = dict(id=sid, revision=revision, project_id=pid, name=scrub(name),
                        documents=docs, transport=transport, imported_at=now(), actor=actor,
                        sha256=hashlib.sha256(encoded(docs).encode()).hexdigest())
            db.execute('INSERT INTO data_source_versions VALUES(?,?,?)', (sid, revision, encoded(body)))
            db.execute('INSERT INTO data_sources VALUES(?,?,?,1) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision',
                       (sid, pid, revision))
            db.execute('INSERT INTO data_source_audit(source_id,actor,action,at) VALUES(?,?,?,?)',
                       (sid, actor, f'import:{revision}', now()))
        return self.summary(body)

    @staticmethod
    def summary(body):
        return {**{k: v for k, v in body.items() if k != 'documents'}, 'document_count': len(body['documents'])}

    def list(self, pid):
        self.store.project(pid)
        with self.store.connect() as db:
            rows = db.execute('SELECT v.data,s.enabled FROM data_sources s JOIN data_source_versions v '
                              'ON s.id=v.id AND s.revision=v.revision WHERE s.project_id=? ORDER BY s.id', (pid,)).fetchall()
        return [{**self.summary(json.loads(r['data'])), 'enabled': bool(r['enabled'])} for r in rows]

    def resolve(self, pid, ref):
        source_refs([ref])
        with self.store.connect() as db:
            row = db.execute('SELECT v.data,s.enabled FROM data_sources s JOIN data_source_versions v ON s.id=v.id '
                             'WHERE s.project_id=? AND s.id=? AND v.revision=?', (pid, ref['id'], ref['revision'])).fetchone()
        if not row or not row['enabled']:
            raise ValueError('数据源不存在、已停用或未授权给该项目')
        return json.loads(row['data'])

    def bind(self, pid, slot, ref, expected_revision, actor):
        source_slots([slot])
        self.resolve(pid, ref)
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(actor, str) or not actor.strip():
            raise ValueError('槽位绑定版本或配置人无效')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM project_source_slots WHERE project_id=? AND slot=?', (pid, slot)).fetchone()
            if (row[0] if row else 0) != expected_revision:
                raise Conflict('项目数据源绑定已变化')
            db.execute('INSERT INTO project_source_slots VALUES(?,?,?,?,?) ON CONFLICT(project_id,slot) DO UPDATE SET '
                       'revision=excluded.revision,source_id=excluded.source_id,source_revision=excluded.source_revision',
                       (pid, slot, expected_revision + 1, ref['id'], ref['revision']))
            db.execute('INSERT INTO data_source_audit(source_id,actor,action,at) VALUES(?,?,?,?)',
                       (ref['id'], actor, f'bind:{pid}:{slot}:{ref["revision"]}', now()))
        return {'slot': slot, 'revision': expected_revision + 1, 'source': ref}

    def resolve_slots(self, pid, slots):
        refs = []
        for slot in source_slots(slots):
            with self.store.connect() as db:
                row = db.execute('SELECT source_id,source_revision FROM project_source_slots WHERE project_id=? AND slot=?', (pid, slot)).fetchone()
            if not row:
                raise ValueError(f'请先为项目绑定资料槽位：{slot}')
            ref = {'id': row[0], 'revision': row[1]}
            self.resolve(pid, ref)
            refs.append(ref)
        return refs

    def enable(self, pid, sid, enabled, actor):
        if type(enabled) is not bool or not isinstance(actor, str) or not actor.strip():
            raise ValueError('数据源启停参数无效')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('UPDATE data_sources SET enabled=? WHERE id=? AND project_id=?', (int(enabled), sid, pid))
            if not cursor.rowcount:
                raise KeyError(sid)
            db.execute('INSERT INTO data_source_audit(source_id,actor,action,at) VALUES(?,?,?,?)',
                       (sid, actor, 'enabled' if enabled else 'disabled', now()))
