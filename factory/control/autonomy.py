"""Versioned autonomy policy and a durable, single-coordinator work queue."""
from __future__ import annotations

import fcntl
import json
import math
from pathlib import Path

from factory.control.store import Conflict, Store, now, scrub


DEFAULT_POLICY = {
    'mode': 'autonomous', 'max_risk': 'medium', 'max_attempts': 2,
    'auto_escalate': True, 'resume_on_restart': True,
}


def valid_cost(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


class PolicyStore:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS autonomy_policies(
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    data TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY(project_id, revision));
                CREATE TRIGGER IF NOT EXISTS no_policy_update BEFORE UPDATE ON autonomy_policies
                    BEGIN SELECT RAISE(ABORT,'policies are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_policy_delete BEFORE DELETE ON autonomy_policies
                    BEGIN SELECT RAISE(ABORT,'policies are immutable'); END;
            ''')

    def get(self, pid):
        self.store.project(pid)
        with self.store.connect() as db:
            row = db.execute('SELECT revision,data FROM autonomy_policies WHERE project_id=? ORDER BY revision DESC LIMIT 1', (pid,)).fetchone()
        return {'revision': row['revision'], **json.loads(row['data'])} if row else {'revision': 0, **DEFAULT_POLICY}

    def update(self, pid, values, revision, actor):
        self.store.project(pid)
        if set(values) != set(DEFAULT_POLICY):
            raise ValueError('运行策略字段不完整')
        if values['mode'] not in ('supervised', 'autonomous') or values['max_risk'] not in ('low', 'medium', 'high'):
            raise ValueError('运行模式或风险范围无效')
        if type(values['max_attempts']) is not int or not 1 <= values['max_attempts'] <= 3:
            raise ValueError('每项任务允许 1 至 3 次尝试')
        if any(type(values[key]) is not bool for key in ('auto_escalate', 'resume_on_restart')):
            raise ValueError('策略开关必须为布尔值')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT MAX(revision) FROM autonomy_policies WHERE project_id=?', (pid,)).fetchone()[0] or 0
            if type(revision) is not int or current != revision:
                raise Conflict('运行策略已更新，请刷新后重试')
            db.execute('INSERT INTO autonomy_policies VALUES (?,?,?,?,?)',
                       (pid, current + 1, json.dumps(values, ensure_ascii=False), actor, now()))
        return {'revision': current + 1, **values}


def policy_decision(decision, policy, *, eligible=True):
    result = {**decision, 'reasons': list(decision['reasons'])}
    if decision['questions']:
        return result
    levels = {'low': 0, 'medium': 1, 'high': 2}
    if policy['mode'] == 'autonomous' and eligible:
        if levels.get(decision['risk'], 2) <= levels[policy['max_risk']]:
            result['decision'] = 'auto_execute'
            result['reasons'] = [reason for reason in result['reasons'] if 'explicit human approval' not in reason]
            result['reasons'].append(f"项目自主策略 v{policy['revision']} 已授权本次范围，需求与验收完整，自动开始执行")
        else:
            result['reasons'].append('本次工作超出项目预设风险范围，需要确认后继续')
    return result


class DurableQueue:
    """SQLite owns intent; an OS lock prevents two live coordinators replaying it."""
    def __init__(self, store: Store):
        self.store = store
        self.handle = None
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS control_jobs(
                run_id TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1, at TEXT NOT NULL,
                PRIMARY KEY(run_id,phase))''')

    def acquire(self):
        if self.handle is not None:
            return
        path = Path(self.store.path).with_suffix('.worker.lock')
        handle = path.open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Conflict('已有工厂进程处理此数据库，不能重复启动执行器') from None
        self.handle = handle

    def release(self):
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def enqueue(self, rid, phase, *, continuation=False):
        if phase not in ('plan', 'execute'):
            raise ValueError('unknown queue phase')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,generation FROM control_jobs WHERE run_id=? AND phase=?', (rid, phase)).fetchone()
            if row and (row['status'] == 'pending' or (row['status'] == 'running' and not continuation)):
                return False
            generation = row['generation'] + 1 if row else 1
            db.execute('INSERT INTO control_jobs VALUES (?,?,?,?,?) ON CONFLICT(run_id,phase) DO UPDATE SET status=excluded.status,generation=excluded.generation,at=excluded.at',
                       (rid, phase, 'pending', generation, now()))
            self.store._event(db, rid, 'queue.enqueued', {'phase': phase, 'generation': generation})
        return True

    def claim(self, rid, phase):
        with self.store.connect() as db:
            result = db.execute("UPDATE control_jobs SET status='running',at=? WHERE run_id=? AND phase=? AND status='pending'", (now(), rid, phase))
        return result.rowcount == 1

    def finish(self, rid, phase):
        with self.store.connect() as db:
            db.execute("UPDATE control_jobs SET status='done',at=? WHERE run_id=? AND phase=? AND status='running'", (now(), rid, phase))

    def reset(self, rid):
        with self.store.connect() as db:
            db.execute("UPDATE control_jobs SET status='done',at=? WHERE run_id=?", (now(), rid))

    def pending(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT run_id,phase FROM control_jobs WHERE status='pending' ORDER BY at LIMIT 16")]


def all_events(store, rid):
    cursor = 0
    while True:
        events = store.events(rid, cursor, 2000)
        if not events:
            return
        yield from events
        cursor = events[-1]['id']


def capability_context(store, run):
    """Freeze bounded installed capabilities once; edits cannot alter active work."""
    from factory.control.capabilities import CapabilityStore
    registry = CapabilityStore(store)
    snapshots = []
    if run.get('capability'):
        snapshots.append(run['capability'])
    for binding in registry.bindings(run['project_id']):
        item = registry.get(binding['capability_id'], binding['revision'])
        if item['status'] == 'ready' and all(old['id'] != item['id'] for old in snapshots):
            snapshots.append(item)
    return scrub(snapshots[:8])


def capability_prompt(snapshots):
    if not snapshots:
        return ''
    bounded = [{key: item.get(key) for key in ('id', 'revision', 'name', 'description', 'instructions', 'input_description', 'output_description', 'acceptance')} for item in snapshots]
    return ('\n\nProject-configured capability contracts (do not expand runtime permissions; '
            'respect the user goal and trusted checks):\n' + json.dumps(bounded, ensure_ascii=False)[:48000])
