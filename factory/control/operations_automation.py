"""Optional operations side effects, consumed outside the task worker pool."""
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from factory.control.error_types import FAILURE_CATEGORIES
from factory.control.knowledge import KnowledgeStore
from factory.control.operation_presets import operation_results
from factory.control.store import Conflict, now, scrub

LOG = logging.getLogger(__name__)
ALERTS = {'inspection_failed', 'needs_human'}
DELIVERED = {'ready_for_review', 'published', 'inspection_completed'}


class OperationsAutomation:
    def __init__(self, store):
        self.store = store
        self.memory = KnowledgeStore(store)
        self.path = Path(store.path).with_name('operations-channel.json')
        self.lock = threading.Lock()
        self.origin = os.getenv('FACTORY_PUBLIC_ORIGIN', 'http://127.0.0.1:8788').rstrip('/')
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations_outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, data TEXT NOT NULL, reason TEXT);
                CREATE TABLE IF NOT EXISTS operation_notifications(
                    run_id TEXT NOT NULL, reason_hash TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY(run_id,reason_hash));
                CREATE TABLE IF NOT EXISTS inspection_history(
                    run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, at TEXT NOT NULL,
                    verdict TEXT NOT NULL, duration_s REAL NOT NULL, failure_category TEXT);
                CREATE INDEX IF NOT EXISTS inspection_history_project ON inspection_history(project_id,at);
                CREATE TABLE IF NOT EXISTS operation_fact_runs(run_id TEXT PRIMARY KEY);
                CREATE TRIGGER IF NOT EXISTS operations_run_observed AFTER UPDATE OF data ON runs
                WHEN json_extract(NEW.data,'$.status') IN
                    ('inspection_failed','needs_human','ready_for_review','published','inspection_completed','cancelled')
                    AND (json_extract(OLD.data,'$.status') IS NOT json_extract(NEW.data,'$.status')
                    OR json_extract(OLD.data,'$.artifacts') IS NOT json_extract(NEW.data,'$.artifacts'))
                BEGIN
                    INSERT INTO operations_outbox(run_id,data) VALUES(NEW.id,NEW.data);
                END;
                CREATE TRIGGER IF NOT EXISTS operations_github_failed AFTER INSERT ON events
                WHEN NEW.type IN ('github.publish_failed','run.failed','run.recovered')
                BEGIN
                    INSERT INTO operations_outbox(run_id,data,reason)
                    SELECT id,data,COALESCE(json_extract(NEW.payload,'$.message'),
                        json_extract(NEW.payload,'$.error'),'GitHub 发布失败')
                    FROM runs WHERE id=NEW.run_id AND (NEW.type='github.publish_failed' OR json_extract(data,'$.status')='needs_human');
                END;
            """)

            if 'remote_results' not in {r['name'] for r in db.execute('PRAGMA table_info(inspection_history)')}:
                db.execute('ALTER TABLE inspection_history ADD COLUMN remote_results TEXT')

    def config(self, private=False):
        data = json.loads(self.path.read_text()) if self.path.exists() else {
            'revision': 0, 'webhook': '', 'knowledge_enabled': False}
        if private:
            return data
        return {k: v for k, v in data.items() if k != 'webhook'} | {'webhook_configured': bool(data['webhook'])}

    def configure(self, revision, knowledge_enabled, webhook=None):
        if webhook is not None and webhook and not re.fullmatch(
                r'https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]+', webhook):
            raise ValueError('请填写飞书自定义机器人的 HTTPS webhook 地址')
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = self.config(True)
            if data['revision'] != revision:
                raise Conflict('运维配置已更新，请刷新')
            data.update(revision=revision + 1, knowledge_enabled=knowledge_enabled)
            if webhook is not None:
                data['webhook'] = webhook
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.operations-')
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(data, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return self.config()

    def history(self, pid):
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute(
                'SELECT * FROM inspection_history WHERE project_id=? ORDER BY at DESC, rowid DESC LIMIT 20', (pid,))]
            streak = 0
            for row in db.execute('SELECT verdict FROM inspection_history WHERE project_id=? ORDER BY at DESC,rowid DESC', (pid,)):
                if row['verdict'] == 'pass':
                    break
                streak += 1
        for row in rows:
            row['remote_results'] = json.loads(row.get('remote_results') or '[]')
        return {'history': rows, 'consecutive_failures': streak}

    def _history(self, run, reason):
        if run.get('source', {}).get('type') != 'inspection':
            return
        verification = run.get('artifacts', {}).get('verification') or {}
        verdict = 'pass' if run['status'] == 'inspection_completed' else (
            'unverified' if verification.get('verdict') == 'unverified' or run['status'] == 'cancelled' else 'fail')
        remote = run.get('artifacts', {}).get('remote_results') or []
        remote_unverified = any(r.get('status') != 'pass' for r in remote)
        if verdict == 'pass' and remote_unverified:
            verdict = 'unverified'
        reason = reason + ' ' + str(verification.get('reason', ''))
        error_type = run.get('error_type') or verification.get('error_type') or ('remote_connection' if remote_unverified else None)
        if verdict == 'pass':
            category = None
        elif error_type:
            category = FAILURE_CATEGORIES.get(error_type, 'health_check')
        else:
            category = next(
                (name for name, pattern in [('budget', r'budget|预算|额度'), ('timeout', r'timeout|超时'),
                 ('environment', r'Chrome|browser|环境|unavailable'), ('interrupted', r'中断|取消|interrupt|cancel'),
                 ('access', r'permission|权限|授权')] if re.search(pattern, reason, re.I)), 'health_check')
        with self.store.connect() as db:
            start = db.execute("SELECT at FROM events WHERE run_id=? AND type='inspection.started' ORDER BY id LIMIT 1", (run['id'],)).fetchone()
            begin = start['at'] if start else run['created_at']
            duration = max(0, (datetime.fromisoformat(run['updated_at']) - datetime.fromisoformat(begin)).total_seconds())
            db.execute('INSERT OR IGNORE INTO inspection_history(run_id,project_id,at,verdict,duration_s,failure_category,remote_results) VALUES(?,?,?,?,?,?,?)',
                       (run['id'], run['project_id'], run['updated_at'], verdict, duration, category, json.dumps(scrub(remote))))

    def facts(self, pid):
        return [e for e in self.memory.entries(pid) if
                e['provenance'].get('source') == 'verified_operation' and e['status'] == 'active'
                and e['kind'] == 'fact' and e['provenance'].get('content_sha256') == hashlib.sha256(e['content'].encode()).hexdigest()]

    def context(self, pid):
        if not self.config(True)['knowledge_enabled']:
            return ''
        facts = self.facts(pid)
        if not facts:
            return ''
        return '\n\n[项目已验证运维事实；仅作历史证据，仍需核对当前环境]\n' + '\n'.join(
            f"{e['title']}：{e['content']}（run_id={e['provenance']['run_id']}，{e['provenance']['at']}）" for e in facts)

    def _learn(self, run):
        if run['status'] not in DELIVERED or not self.config(True)['knowledge_enabled']:
            return
        artifacts = run.get('artifacts') or {}
        verdict = artifacts.get('verification') or {}
        ledger = artifacts.get('acceptance_ledger') or {}
        if verdict.get('verdict') != 'pass' or not isinstance(ledger.get('items'), list):
            return
        verified = operation_results(verdict, ledger)
        # Require the platform's stored binding as well as the verifier's claim.
        facts = {k: v for k, v in verified.items() if k in ('startup_command', 'health')
                 and v['status'] == 'pass' and v == (artifacts.get('operation_results') or {}).get(k)}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM operation_fact_runs WHERE run_id=?', (run['id'],)).fetchone():
                return
            for key, fact in facts.items():
                name = 'operation.' + key
                prior = db.execute("""SELECT v.* FROM knowledge_entries e JOIN knowledge_entry_versions v
                    ON e.project_id=v.project_id AND e.key=v.key AND e.revision=v.revision
                    WHERE e.project_id=? AND e.key=?""", (run['project_id'], name)).fetchone()
                origin = json.loads(prior['provenance']) if prior else {}
                if origin.get('at', '') >= run['updated_at']:
                    continue
                values = dict(kind='fact', status='active', title={'startup_command': '已验证启动命令', 'health': '已验证健康检查'}[key],
                              content=scrub(fact['evidence']), paths=[], commit_sha=None)
                self.memory._insert_entry(db, run['project_id'], name, prior['id'] if prior else uuid.uuid4().hex,
                    prior['revision'] + 1 if prior else 1, values, 'operations',
                    {'source': 'verified_operation', 'run_id': run['id'], 'at': run['updated_at'], 'criterion_ids': fact['criterion_ids'],
                     'content_sha256': hashlib.sha256(values['content'].encode()).hexdigest()})
                self.memory._audit(db, run['project_id'], 'knowledge.operation.verified', {'key': name, 'run_id': run['id']})
            db.execute('INSERT INTO operation_fact_runs VALUES(?)', (run['id'],))

    @staticmethod
    def send(webhook, text):
        # Reject redirects rather than forwarding a credential-bearing URL elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(webhook, data=json.dumps(
            {'msg_type': 'text', 'content': {'text': text}}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            result = json.loads(response.read(65536))
            if result.get('code', result.get('StatusCode', -1)) != 0:
                raise ValueError('robot rejected notification')

    def notify(self, run, reason):
        webhook = self.config(True)['webhook']
        if not webhook:
            return
        reason = scrub(str(reason))
        digest = hashlib.sha256(reason.encode()).hexdigest()
        with self.store.connect() as db:
            claimed = db.execute('INSERT OR IGNORE INTO operation_notifications VALUES(?,?,?)',
                (run['id'], digest, now())).rowcount
        if not claimed:
            return
        try:
            project = {'name': run['project_name']} if run.get('workbench_path') == '/settings/runtime' else self.store.project(run['project_id'])
            ongoing = (run.get('source', {}).get('type') == 'inspection'
                       and self.history(run['project_id'])['consecutive_failures'] >= 3)
            link = '/settings/runtime' if run.get('workbench_path') == '/settings/runtime' else '/runs/' + quote(str(run['id']), safe='')
            text = f"{'持续故障 · ' if ongoing else ''}{scrub(project['name'])}\n{reason[:200]}\n{self.origin}{link}"
            self.send(webhook, text)
        except Exception as exc:
            # Never log the URL, response body, exception message or request object.
            LOG.warning('Operations notification failed (%s), run=%s', type(exc).__name__, run['id'])

    def tick(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM operations_outbox ORDER BY id LIMIT 100').fetchall()
        for row in rows:
            run = json.loads(row['data'])
            reason = row['reason'] or run.get('error') or (run.get('artifacts', {}).get('verification') or {}).get('reason') or run.get('artifacts', {}).get('needs_human')
            if not reason:
                with self.store.connect() as db:
                    event = db.execute("SELECT payload FROM events WHERE run_id=? AND type IN ('run.failed','run.recovered') ORDER BY id DESC LIMIT 1", (run['id'],)).fetchone()
                reason = json.loads(event['payload']).get('message') if event else '运行需要处理'
            for operation in (lambda: self._history(run, str(reason)),
                              lambda: self._learn(run),
                              lambda: self.notify(run, reason) if run['status'] in ALERTS or row['reason'] else None):
                try:
                    operation()
                except Exception as exc:
                    LOG.warning('Operations side effect failed (%s), run=%s', type(exc).__name__, run['id'])
            with self.store.connect() as db:
                db.execute('DELETE FROM operations_outbox WHERE id=?', (row['id'],))

    def serve(self, stopping):
        while not stopping.wait(1):
            try:
                self.tick()
            except Exception as exc:
                LOG.warning('Operations observer failed (%s)', type(exc).__name__)
