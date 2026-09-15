"""Local, opt-in inspection scheduling on the existing durable worker queue."""
import json
import time
import uuid
from pathlib import Path

from factory.control.operation_presets import OperationStore
from factory.control.execution import ExecutionError
from factory.control.error_types import failure_type
from factory.control.store import ACTIVE, Conflict, now, scrub


class InspectionStore:
    def __init__(self, store, operations=None, automation=None, targets=None):
        self.store = store
        self.automation = automation
        self.targets = targets
        self.operations = operations if operations is not None else OperationStore(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS project_inspections(
                project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, interval_s INTEGER NOT NULL,
                revision INTEGER NOT NULL, actor_id INTEGER, next_at REAL NOT NULL,
                last_at TEXT, last_run_id TEXT)''')

            if 'remote_read_only' not in {r['name'] for r in db.execute('PRAGMA table_info(project_inspections)')}:
                db.execute('ALTER TABLE project_inspections ADD COLUMN remote_read_only INTEGER NOT NULL DEFAULT 0')

    def get(self, pid):
        self.store.project(pid)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM project_inspections WHERE project_id=?', (pid,)).fetchone()
        data = dict(row) if row else dict(project_id=pid, enabled=False, interval_s=3600,
            revision=0, next_at=0, last_at=None, last_run_id=None)
        data['enabled'] = bool(data['enabled'])
        data['remote_read_only'] = bool(data.get('remote_read_only', False))
        if data.get('last_run_id'):
            run = self.store.get(data['last_run_id'])
            data['last_status'] = run['status']
            data['last_result'] = (run.get('artifacts', {}).get('verification') or {}).get('reason') or run.get('error')
            remote = run.get('artifacts', {}).get('remote_results') or []
            unresolved = [r for r in remote if r.get('status') != 'pass']
            if unresolved:
                data['last_result'] = '远程探测未验证：' + '; '.join(r.get('reason', '') for r in unresolved)[:500]
        if self.automation is not None:
            data.update(self.automation.history(pid))
        return data

    def configure(self, pid, *, enabled, interval_s, revision, actor_id, remote_read_only=False):
        project = self.store.project(pid)
        if not isinstance(remote_read_only, bool):
            raise ValueError('远程只读探测须为开关')
        if enabled and not Path(project['workspace']).is_dir():
            raise ValueError('巡检仅支持本机可达的项目工作区')
        if not isinstance(enabled, bool) or not isinstance(interval_s, int) or not 300 <= interval_s <= 604800:
            raise ValueError('巡检间隔须为 300 至 604800 秒')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM project_inspections WHERE project_id=?', (pid,)).fetchone()
            if (row['revision'] if row else 0) != revision:
                raise Conflict('巡检配置已变化，请刷新后重试')
            db.execute('''INSERT INTO project_inspections(project_id,enabled,interval_s,revision,actor_id,next_at,last_at,last_run_id,remote_read_only) VALUES(?,?,?,?,?,?,NULL,NULL,?)
                ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,interval_s=excluded.interval_s,
                revision=excluded.revision,actor_id=excluded.actor_id,next_at=excluded.next_at,remote_read_only=excluded.remote_read_only''',
                (pid, enabled, interval_s, revision + 1, actor_id, time.time() + interval_s, remote_read_only))
            db.execute('INSERT INTO project_settings_audit(project_id,revision,actor,action,data,at) VALUES(?,?,?,?,?,?)',
                (pid, revision+1, str(actor_id), 'inspection.configure', json.dumps({'enabled': enabled, 'interval_s': interval_s, 'remote_read_only': remote_read_only}), now()))
        return self.get(pid)

    def tick(self, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        compiled, _, preset = self.operations.compile('startup', '定时巡检：检查本机项目副本能否按已有说明启动并通过健康检查。')
        # Override startup repair instructions: inspection has no execution phase.
        prompt = ('只巡检，不修复、不部署、不发布；不得改动原项目、既有源码、依赖声明或测试。'
                  '在隔离副本运行已有启动命令与健康检查，缺配置或无法验证须标记 unverified。'
                  '只报告观察与具体问题。以下启动排错说明只采用诊断步骤，不采用修复步骤：\n' + compiled)
        created = []
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            due = db.execute('SELECT * FROM project_inspections WHERE enabled=1 AND next_at<=? ORDER BY next_at LIMIT 16', (timestamp,)).fetchall()
            for config in due:
                pid = config['project_id']
                busy = db.execute("SELECT 1 FROM runs WHERE json_extract(data,'$.project_id')=? AND json_extract(data,'$.status') IN (%s) LIMIT 1" % ",".join("?" for _ in ACTIVE), (pid, *ACTIVE)).fetchone()
                if busy:
                    continue
                rid = uuid.uuid4().hex
                context = self.automation.context(pid) if self.automation is not None else ''
                run = dict(id=rid, project_id=pid, request=prompt + context, status='received', revision=0,
                    plan=None, triage=None, tasks=[], artifacts={}, history=[], created_at=now(), updated_at=now(),
                    source={'type': 'inspection', 'operation': 'startup', 'operation_version': preset['version'],
                            'actor_id': config['actor_id'], 'schedule_revision': config['revision'],
                            'remote_read_only': bool(config['remote_read_only']),
                            'remote_targets': self.targets.snapshot(pid) if config['remote_read_only'] and self.targets else []})
                # Supersession and enqueue share one transaction; never drop a finding
                # unless its replacement is durably queued. Preserve all prior evidence.
                older = db.execute("SELECT id,data FROM runs WHERE json_extract(data,'$.project_id')=? AND json_extract(data,'$.source.type')='inspection' AND json_extract(data,'$.status') IN ('inspection_failed','needs_human')", (pid,)).fetchall()
                for row in older:
                    previous = json.loads(row['data'])
                    if previous.get('inspection_superseded_by'):
                        continue
                    if not previous.get('error'):
                        failure = db.execute("SELECT payload FROM events WHERE run_id=? AND type='run.failed' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
                        if failure:
                            previous['error'] = json.loads(failure['payload']).get('message')
                    previous.update(status='inspection_failed', inspection_superseded_by=rid, updated_at=now())
                    db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(previous, ensure_ascii=False), row['id']))
                    self.store._event(db, row['id'], 'inspection.superseded', {'replacement_run_id': rid})
                db.execute('INSERT INTO runs VALUES(?,?)', (rid, json.dumps(scrub(run), ensure_ascii=False)))
                db.execute('INSERT INTO control_jobs VALUES(?,?,?,?,?)', (rid, 'plan', 'pending', 1, now()))
                db.execute('UPDATE project_inspections SET next_at=?,last_at=?,last_run_id=? WHERE project_id=?',
                    (timestamp + config['interval_s'], now(), rid, pid))
                self.store._event(db, rid, 'inspection.enqueued', {'message': '本机隔离巡检，不自动修复或部署'})
                created.append(rid)
        return created


def inspect_run(service, rid):
    """One budgeted verification session; never enter coding, repair, or publication."""
    run = service.store.get(rid)
    artifacts = {}
    try:
        project = service.store.project(run['project_id'])
        service._remaining_dollar_budget(rid, project)
        if service.governance is not None:
            service.governance.require_project(run['source']['actor_id'], run['project_id'])
        service.store.update(rid, {'status': 'verifying'}, expected=('received',),
            event=('inspection.started', {'message': '正在检查隔离项目副本'}))
        service._independent_verify(rid, run, project, service.runtime_settings.get(), artifacts)
        if run['source'].get('remote_read_only'):
            service.remote.collect(rid, artifacts, inspection=True)
        service.store.update(rid, {'status': 'inspection_completed', 'artifacts': artifacts},
            expected=('verifying',), event=('inspection.completed', {'message': '巡检通过；没有改代码或部署'}))
    except Exception as exc:
        if run['source'].get('remote_read_only') and service.store.get(rid)['status'] == 'verifying' and 'remote_results' not in artifacts:
            service.remote.collect(rid, artifacts, inspection=True)
        service._fail(rid, ExecutionError(str(exc), artifacts=artifacts, error_type=failure_type(exc)))
