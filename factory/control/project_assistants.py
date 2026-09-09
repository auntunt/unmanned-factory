"""Project helpers and explicit, source-preserving knowledge disposition."""
from __future__ import annotations

import copy
import json

from factory.control.agents import AgentStore
from factory.control.capabilities import CapabilityStore
from factory.control.knowledge import KnowledgeStore
from factory.control.store import Conflict, now


class ProjectAssistants:
    def __init__(self, store):
        self.store = store
        self.agents = AgentStore(store)
        self.memory = KnowledgeStore(store)
        self.capabilities = CapabilityStore(store)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS project_assistants(
                    project_id TEXT PRIMARY KEY, agent_id TEXT, revision INTEGER NOT NULL,
                    actor TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_dispositions(
                    project_id TEXT NOT NULL, source_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(project_id, source_id));
            ''')

    def binding(self, pid):
        self.store.project(pid)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM project_assistants WHERE project_id=?', (pid,)).fetchone()
        result = dict(row) if row else {'project_id': pid, 'agent_id': None, 'revision': 0}
        if result['agent_id']:
            aid = result['agent_id']
            result['agent'] = {**self.agents.get(aid), 'version': self.agents.version(aid)}
            with self.store.connect() as db:
                result['skills'] = [json.loads(r[0]) for r in db.execute('SELECT data FROM skill_assets WHERE agent_id=?', (aid,))]
        return result

    def bind(self, pid, aid, revision, actor):
        self.store.project(pid)
        if aid:
            self.agents.get(aid)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM project_assistants WHERE project_id=?', (pid,)).fetchone()
            if (row[0] if row else 0) != revision:
                raise Conflict('项目职能体已变更，请刷新后再选择')
            db.execute('INSERT OR REPLACE INTO project_assistants VALUES (?,?,?,?,?)', (pid, aid, revision + 1, str(actor), now()))
        return self.binding(pid)

    def freeze(self, run, runtime):
        if run.get('agent_snapshot'):
            return {}
        binding = self.binding(run['project_id'])
        if not binding.get('agent'):
            return {}
        agent = binding['agent']; version = agent['version']
        config = copy.deepcopy(runtime)
        overrides = version.get('model_settings') or {}
        def configured(*names):
            return next((overrides[n] for n in names if isinstance(overrides.get(n), dict) and overrides[n].get('model') and overrides[n].get('provider')), None)
        for roles, names in [(('planner',), ('planning', 'analysis', 'default')), (('cheap', 'standard', 'strong'), ('execution', 'default'))]:
            value = configured(*names)
            if value:
                for role in roles:
                    config['profiles'][role] = {k: value[k] for k in ('provider', 'model')}
        verification = configured('verification', 'default')
        if verification:
            config['agent_verification_profile'] = verification
        return {'agent_id': agent['id'], 'agent_version': version['version'], 'agent_snapshot': version,
                'runtime_configuration': config, 'project_assistant_revision': binding['revision']}

    def learnings(self, pid):
        self.store.project(pid)
        entries = [{'id': 'knowledge:' + e['key'], 'revision': e['revision'], 'title': e['title'], 'content': e['content'],
                    'kind': e['kind'], 'status': e['status'], 'source': '项目知识', 'commit_sha': e.get('commit_sha')}
                   for e in self.memory.entries(pid)]
        runs = {r['id']: r for r in self.store.all_runs() if r['project_id'] == pid}
        for c in self.capabilities.list():
            if c.get('source_run_id') in runs:
                entries.append({'id': 'capability:' + c['id'], 'revision': c['revision'], 'title': c['name'], 'content': c['instructions'],
                                'source': '交付收获', 'status': 'candidate', 'run_id': c['source_run_id'],
                                'agent_id': runs[c['source_run_id']].get('agent_id')})
        with self.store.connect() as db:
            dispositions = {r[0]: json.loads(r[1]) for r in db.execute('SELECT source_id,data FROM learning_dispositions WHERE project_id=?', (pid,))}
        for entry in entries:
            saved = dispositions.get(entry['id'])
            if saved:
                entry['disposition'] = saved
                if saved.get('agent_id'):
                    target = self.agents.get(saved['agent_id'])
                    entry['disposition'] = {**saved, 'agent_name': target['name'],
                        'applied': saved['marker'] in self.agents.version(target['id'])['instructions']}
        return entries

    def settle(self, pid, source_id, source_revision, destination, aid, draft_revision, actor):
        source = next((e for e in self.learnings(pid) if e['id'] == source_id), None)
        if not source:
            raise KeyError(source_id)
        if source['revision'] != source_revision:
            raise Conflict('收获内容已更新，请刷新后再沉淀')
        if destination == 'agent' and not aid:
            raise ValueError('请选择要合并的职能体')
        record = {'destination': destination, 'agent_id': aid if destination == 'agent' else None,
                  'source_revision': source_revision, 'source': {k: v for k, v in source.items() if k != 'disposition'},
                  'actor': str(actor), 'created_at': now()}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT data FROM learning_dispositions WHERE project_id=? AND source_id=?', (pid, source_id)).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved['destination'] == destination and saved.get('agent_id') == record['agent_id'] and saved['source_revision'] == source_revision:
                    return saved
                raise Conflict('这条收获已有沉淀归属，请保留原记录并通过新条目补充')
            if destination == 'agent':
                # Keep source provenance and existing unresolved draft conflicts.
                draft = self.agents.draft(aid)
                marker = f'[项目收获 {pid}/{source_id}@{source_revision}]'
                instructions = draft['patch']['instructions'] + '\n\n' + marker + '\n' + source['title'] + '\n' + source['content']
                result = self.agents.save_draft(aid, {'instructions': instructions}, draft_revision,
                    conflicts=draft.get('conflicts'), explanation=[*draft.get('explanation', []), f"来自项目 {pid}：{source['title']}"], _db=db)
                record.update(marker=marker, draft_revision=result['revision'])
            db.execute('INSERT INTO learning_dispositions VALUES (?,?,?)', (pid, source_id, json.dumps(record, ensure_ascii=False)))
        return record
