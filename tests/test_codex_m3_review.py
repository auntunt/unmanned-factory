import runpy
import subprocess
import sys
from pathlib import Path
from factory.control.store import Store
from factory.control.service import Service
from factory.control.issue_maintenance import MaintenanceTasks

H = runpy.run_path(str(Path.cwd() / 'tests/test_issue_maintenance_core.py'))

def test_cli_read_does_not_interrupt_live_maintenance_job(tmp_path):
    store = Store(tmp_path / 'control.db')
    svc = Service(store, runner=object())
    try:
        with store.connect() as db:
            db.execute("INSERT INTO maintenance_jobs(id,conversation_id,actor_id,status,created_at,updated_at) VALUES ('live-job','chat',1,'running','t','t')")
        result = subprocess.run([sys.executable, '-m', 'factory.control.maintenance_cli', '--db', str(store.path), 'list', '--project', 'p1'], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        with store.connect() as db:
            status = db.execute("SELECT status FROM maintenance_jobs WHERE id='live-job'").fetchone()[0]
        assert status == 'running', 'read-only CLI must not run service restart recovery against live jobs'
    finally:
        svc.close()

def test_retry_after_submit_failure_eventually_binds_execution(tmp_path):
    store = Store(tmp_path / 'control.db')
    ex = H['_Execution']()
    original = ex.submit
    attempts = []
    def fail_once(record, *, actor):
        attempts.append(record['id'])
        if len(attempts) == 1:
            raise RuntimeError('dispatch unavailable before any execution')
        return original(record, actor=actor)
    ex.submit = fail_once
    tasks = MaintenanceTasks(store, execution=ex, repository=H['_Repository'](), identity=H['_Identity']())
    request = H['_request']()
    try:
        tasks.create(request, actor=H['ACTOR'])
    except RuntimeError:
        pass
    view = tasks.create(request, actor=H['ACTOR'])
    assert view['execution_id'] is not None, 'retry must reconcile or dispatch the reserved task, not leave a permanent orphan'
