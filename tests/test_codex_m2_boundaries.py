"""Independent M2 boundary probes; real local processes/checks, no paid models."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from factory.control import provider_activity
from factory.control.execution import ExecutionError
from tests.test_control_app import app_env
from tests.test_remote_targets import remote_env, release
from tests.test_remote_action_identity import _lost_response
from tests.test_evidence_reuse_scope import _repo, _project, _plan, _execute, _Runner, _coder, _runs


def test_negative_target_statement_cannot_confirm_deployment(remote_env):
    _, store, svc, _, _, target, config, _ = remote_env
    run = release(remote_env)
    aid, _ = _lost_response(store, run, target)
    data = json.loads(config.read_text())
    config.write_text(json.dumps({**data, 'output': f'active (running)\nNOT APPLIED: {aid}; deployment failed'}))
    result = svc.remote.reconcile(run['id'], target['id'], 'deploy')
    assert result['status'] != 'pass', 'Mentioning an action id is not confirmation of applying it'


def test_verification_only_recovery_checks_environment_identity(tmp_path, monkeypatch):
    root, counter = _repo(tmp_path)
    project = _project(root)
    monkeypatch.setenv('PYTHONPATH', '/original-check-environment')
    artifacts, _ = _execute(project, _plan(), _Runner(_coder()))
    assert _runs(counter) == 1
    monkeypatch.setenv('PYTHONPATH', '/different-check-environment')
    try:
        result, _ = _execute(project, _plan(resume_stage='verification'),
            _Runner(lambda *a, **kw: pytest.fail('verification recovery called coding')),
            resume_artifacts=artifacts)
    except ExecutionError:
        return  # Clear refusal is valid; silently claiming reused verification is not.
    assert _runs(counter) > 1, 'verification-only path reused old pass across an environment change'


def test_dead_sdk_wrapper_does_not_mean_its_writer_child_exited(tmp_path):
    lock = tmp_path / 'activity.lock'
    ready, go, wrote = (tmp_path / n for n in ('ready', 'go', 'wrote'))
    child_code = '''import sys,time
from pathlib import Path
ready,go,wrote=map(Path,sys.argv[1:])
ready.write_text('ready')
while not go.exists(): time.sleep(.01)
wrote.write_text('old SDK descendant still wrote')
time.sleep(30)
'''
    parent_code = '''import subprocess,sys,time
from factory.control import sdk_worker

def fake_sdk(req, emit):
    subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:]],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    time.sleep(30)
sdk_worker._run_claude=fake_sdk
raise SystemExit(sdk_worker.main())
'''
    proc = subprocess.Popen([sys.executable, '-c', parent_code, child_code,
        str(ready), str(go), str(wrote)], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
        req = {'provider': 'claude', 'model': 'fake', 'prompt': 'local child probe',
               'workspace': str(tmp_path), 'activity_lock': str(lock)}
        proc.stdin.write((json.dumps(req)+'\n').encode()); proc.stdin.flush(); proc.stdin.close()
        deadline = time.monotonic()+5
        while not ready.exists() and proc.poll() is None and time.monotonic()<deadline:
            time.sleep(.01)
        assert ready.exists(), 'controlled writer child did not start'
        assert provider_activity.is_active(lock)
        proc.kill(); proc.wait(timeout=5)
        reported_active = provider_activity.is_active(lock)
        go.touch()
        deadline = time.monotonic()+3
        while not wrote.exists() and time.monotonic()<deadline: time.sleep(.01)
        assert reported_active or not wrote.exists(), (
            'Recovery sees no active writer after wrapper SIGKILL, yet its SDK child still writes')
    finally:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait(timeout=5)
