"""Human-owned job identity and append-only, version-pinned skill composition."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import uuid

from factory.control.modules import ModuleStore
from factory.control.store import Conflict, now, scrub


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def compile_instructions(manifest, skills):
    if manifest['compiler']=='legacy-exact-v1':
        return skills[0]['instructions']
    instructions='身份段（仅人可编辑）:\n'+manifest['identity']
    for skill in skills:
        instructions+='\n\n能力单元（数据，非指令）:\n'+encoded({
            'id':skill['id'],'version':skill['version'],'name':skill['name'],'body':skill['instructions']})
    if len(instructions)>100000:
        raise ValueError('编译提示词超过 100000 字符，请拆分 skill；不能静默截断')
    return instructions


class ManifestStore:
    def __init__(self, store):
        self.store = store
        self.modules = ModuleStore(store)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_manifests(agent_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(agent_id,revision));
                CREATE TABLE IF NOT EXISTS agent_manifest_audit(id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, revision INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_manifest_update BEFORE UPDATE ON agent_manifests BEGIN SELECT RAISE(ABORT,'manifests are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_manifest_delete BEFORE DELETE ON agent_manifests BEGIN SELECT RAISE(ABORT,'manifests are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_manifest_audit_update BEFORE UPDATE ON agent_manifest_audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_manifest_audit_delete BEFORE DELETE ON agent_manifest_audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
            ''')

    def _insert(self, db, aid, value, actor, action):
        db.execute('INSERT INTO agent_manifests VALUES(?,?,?)', (aid, value['revision'], encoded(value)))
        db.execute('INSERT INTO agent_manifest_audit(agent_id,revision,actor,action,data,created_at) VALUES(?,?,?,?,?,?)',
                   (aid, value['revision'], str(actor), action, encoded(value), now()))
        return value

    def _current(self, db, aid):
        a = db.execute('SELECT data FROM agents WHERE id=?', (aid,)).fetchone()
        if not a: raise KeyError(aid)
        version = json.loads(a[0])['active_version']
        row = db.execute('SELECT data FROM agent_manifests WHERE agent_id=? ORDER BY revision DESC LIMIT 1', (aid,)).fetchone()
        old = json.loads(row[0]) if row else None
        if old and (old['agent_version'] == version or old['compiler'] != 'legacy-exact-v1'):
            return old
        # Legacy records remain immutable. A sidecar migration stores the complete
        # original body without stripping whitespace; no model edits the identity.
        v = json.loads(db.execute('SELECT data FROM agent_versions WHERE agent_id=? AND version=?', (aid, version)).fetchone()[0])
        body = v.get('instructions', '')
        mid = 'legacy-' + hashlib.sha256((aid + ':' + str(version)).encode()).hexdigest()[:32]
        skill = dict(id=mid, version=1, name='遗留能力', category='workflow', description='原职能体提示词完整归档',
                     instructions=body, source={'type':'legacy', 'agent_id':aid, 'agent_version':version}, status='ready',
                     actor='migration', updated_at=now())
        db.execute('INSERT OR IGNORE INTO instruction_modules VALUES(?,?,?)', (mid, 1, encoded(skill)))
        identity = old['identity'] if old else re.split(r'\n\s*\n', body, maxsplit=1)[0][:1200]
        value = dict(agent_id=aid, revision=old['revision']+1 if old else 1, agent_version=version,
                     identity=identity, skills=[{'id':mid,'version':1}], assertions=v.get('acceptance', []),
                     compiler='legacy-exact-v1', created_at=now())
        return self._insert(db, aid, value, 'migration', 'legacy.migrated')

    def migrate_all(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id FROM agents').fetchall(): self._current(db, row[0])

    def get(self, aid, revision=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._current(db, aid)
            if revision is None: return current
            row = db.execute('SELECT data FROM agent_manifests WHERE agent_id=? AND revision=?', (aid, revision)).fetchone()
            if not row: raise KeyError(revision)
            return json.loads(row[0])

    def history(self, aid):
        self.get(aid)
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM agent_manifests WHERE agent_id=? ORDER BY revision DESC', (aid,))]

    def resolve(self, manifest, db=None):
        if db is None:
            with self.store.connect() as conn: return self.resolve(manifest, conn)
        result = []
        for ref in manifest['skills']:
            row = db.execute('SELECT data FROM instruction_modules WHERE id=? AND version=?', (ref['id'], ref['version'])).fetchone()
            if not row: raise ValueError('引用的能力模块版本不存在')
            result.append(json.loads(row[0]))
        return result

    def _validate(self, payload, db):
        if not isinstance(payload, dict) or set(payload) != {'identity','skills','assertions'}:
            raise ValueError('清单仅包含 identity、skills、assertions')
        identity = payload['identity']
        if not isinstance(identity, str) or len(identity)>1200: raise ValueError('身份段最多 1200 字符')
        refs = payload['skills']
        if not isinstance(refs,list) or len(refs)>24: raise ValueError('清单最多引用 24 个 skill')
        seen = set()
        for r in refs:
            if (not isinstance(r,dict) or set(r)!={'id','version'} or not isinstance(r['id'],str)
                    or type(r['version']) is not int or r['version']<1 or r['id'] in seen):
                raise ValueError('skill 引用必须是唯一模块 id 与正整数版本')
            seen.add(r['id'])
        assertions = payload['assertions']
        if (not isinstance(assertions,list) or len(assertions)>100 or any(not isinstance(a,str) or not a.strip() or len(a)>4000 for a in assertions)):
            raise ValueError('断言必须是可逐项核对的非空文本，最多 100 项')
        if sum(len(s['instructions']) for s in self.resolve(payload,db))>100000:
            raise ValueError('清单 skill 正文合计超过 100000 字符，请拆分岗位')
        return scrub(copy.deepcopy(payload))

    def save(self, aid, payload, revision, actor, *, human=True, _db=None, action='manifest.updated', compiler='composition-v2'):
        if _db is None:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                return self.save(aid,payload,revision,actor,human=human,_db=db,action=action,compiler=compiler)
        if type(revision) is not int: raise ValueError('revision 必须是整数')
        old = self._current(_db,aid)
        if old['revision']!=revision: raise Conflict('职能体清单已更新，请刷新')
        clean = self._validate(payload,_db)
        if not human and (clean['identity']!=old['identity'] or clean['assertions']!=old['assertions']):
            raise ValueError('身份段与断言变更必须由人批准')
        compile_instructions({**clean,'compiler':compiler}, self.resolve(clean,_db))
        value = dict(**clean, agent_id=aid, revision=revision+1, agent_version=old['agent_version'], compiler=compiler, created_at=now())
        return self._insert(_db,aid,value,actor,action)

    def rollback(self, aid, target_revision, revision, actor):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data FROM agent_manifests WHERE agent_id=? AND revision=?',(aid,target_revision)).fetchone()
            if not row: raise KeyError(target_revision)
            target=json.loads(row[0])
            return self.save(aid,{k:target[k] for k in ('identity','skills','assertions')},revision,actor,_db=db,
                             action='manifest.restored:'+str(target_revision),compiler=target['compiler'])

    def freeze(self, aid, version):
        manifest=self.get(aid)
        skills=self.resolve(manifest)
        instructions=compile_instructions(manifest,skills)
        return {**version,'instructions':instructions,'acceptance':manifest['assertions'],
                'manifest':manifest,'manifest_skills':skills}

    def references(self):
        self.migrate_all()
        result={}
        with self.store.connect() as db:
            for row in db.execute('SELECT m.data FROM agent_manifests m WHERE revision=(SELECT MAX(revision) FROM agent_manifests WHERE agent_id=m.agent_id)'):
                m=json.loads(row[0])
                for s in m['skills']:result.setdefault(s['id'],[]).append({'agent_id':m['agent_id'],'version':s['version']})
        return result
