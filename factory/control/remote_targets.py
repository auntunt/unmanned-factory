"""Coordinator-only, pinned SSH verbs; no credentials or command text cross into agents."""
import json
import os
import selectors
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from factory.control.deploy_targets import VERBS, fingerprint
from factory.control.store import Conflict, now
from factory.redact import redact_text

READ_ONLY = frozenset(('health_check', 'service_status', 'fetch_log'))


class RemoteFailure(RuntimeError):
    def __init__(self, reason, kind='connection'):
        super().__init__(reason)
        self.kind = kind


def process(argv, timeout):
    """Bound time and memory, kill descendants even if the SSH parent already exited."""
    env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True, env=env)
    captured = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RemoteFailure('远程操作超时，进程组已终止', 'timeout')
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    captured.extend(chunk)
                    if len(captured) > 262144:
                        raise RemoteFailure('远程输出超限，已停止并丢弃原始输出', 'output_limit')
            proc.wait(timeout=max(.01, deadline - time.monotonic()))
        return proc.returncode, captured.decode(errors='replace')
    except subprocess.TimeoutExpired:
        raise RemoteFailure('远程操作超时，进程组已终止', 'timeout') from None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdout.close()


class RemoteTargets:
    def __init__(self, targets):
        self.targets = targets
        self.store = targets.store
        self.timeout = 30
        self.connect_timeout = 5

    def _ssh(self, target, command, timeout):
        deadline = time.monotonic() + timeout
        key = self.targets.key_path(target['id'])
        # keyscan is discovery only: trust is established exclusively by the admin's pin.
        code, scan = process(['ssh-keyscan', '-T', str(self.connect_timeout), '-p', str(target['port']),
                              target['host']], min(timeout, self.connect_timeout + 1))
        matched = []
        for line in scan.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[0].startswith('#'):
                continue
            try:
                if fingerprint(' '.join(parts[1:])) == target['host_fingerprint']:
                    matched.append('webuddy-target ' + ' '.join(parts[1:]))
            except (ValueError, IndexError):
                continue
        if not matched:
            raise RemoteFailure('无法取得匹配的主机公钥指纹，已拒绝连接', 'host_fingerprint')
        with tempfile.TemporaryDirectory(dir=self.targets.key_dir(), prefix='known-') as directory:
            known = Path(directory) / 'known_hosts'
            known.write_text('\n'.join(matched) + '\n'); known.chmod(0o600)
            argv = ['ssh', '-F', '/dev/null', '-T', '-i', str(key), '-p', str(target['port']),
                    '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=/dev/null',
                    '-o', 'HostKeyAlias=webuddy-target', '-o', 'IdentitiesOnly=yes',
                    '-o', 'IdentityAgent=none', '-o', 'PasswordAuthentication=no',
                    '-o', 'KbdInteractiveAuthentication=no', '-o', 'PreferredAuthentications=publickey',
                    '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                    '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                    '-o', 'PermitLocalCommand=no', '-o', 'ProxyCommand=none',
                    '-o', 'ConnectTimeout=' + str(self.connect_timeout),
                    target['user'] + '@' + target['host'], command]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteFailure('远程操作超时，进程组已终止', 'timeout')
            return process(argv, remaining)

    def _run(self, target, verb, lines=100):
        started = time.monotonic()
        result = {'target_id': target['id'], 'target': target['name'], 'verb': verb,
                  'status': 'unverified', 'exit_code': None, 'executed': False, 'attempts': 0}
        command = target['commands'].get(verb)
        if verb == 'connection_test':
            # Fixed authentication probe; never runs a registered business command.
            command = 'true'
        if not command:
            return {**result, 'duration_s': 0, 'reason': '管理员尚未注册此动作'}
        if verb == 'fetch_log':
            command = 'tail -n ' + str(lines) + ' -- ' + shlex.quote(command)
        if verb == 'health_check' and command.startswith(('http://', 'https://')):
            command = 'curl --fail --silent --show-error --max-time 15 -- ' + shlex.quote(command)
        for attempt in range(2 if verb in READ_ONLY else 1):
            result['attempts'] = attempt + 1
            try:
                code, output = self._ssh(target, command, max(.1, self.timeout - (time.monotonic() - started)))
                clean = redact_text(output)
                if verb == 'fetch_log':
                    clean = '\n'.join(clean.splitlines()[-lines:])
                result.update(exit_code=code, executed=code != 255,
                              summary=clean[:12000], status='pass' if code == 0 else 'unverified',
                              reason='' if code == 0 else '远程动作未成功，退出码 ' + str(code))
                if code == 0 or code != 255:
                    break
            except RemoteFailure as exc:
                result.update(reason=str(exc), error_type=exc.kind)
                if exc.kind != 'connection':
                    break
            except (OSError, Conflict):
                result.update(reason='SSH 运行环境或目标密钥不可用', error_type='environment')
                break
            if time.monotonic() - started >= self.timeout:
                break
        result['duration_s'] = round(time.monotonic() - started, 3)
        return result

    def _record(self, run, result):
        with self.store.connect() as db:
            self.store._event(db, run['id'], 'remote.executed', result)
            if result['status'] != 'pass' and (result['verb'] in ('deploy', 'rollback') or result.get('error_type') == 'host_fingerprint'):
                action = '主机指纹' if result.get('error_type') == 'host_fingerprint' else result['verb']
                reason = f"服务器 {result['target']} · {action}：{result['reason']}"
                db.execute('INSERT INTO operations_outbox(run_id,data,reason) VALUES(?,?,?)',
                           (run['id'], json.dumps(run), redact_text(reason)))

    def test(self, tid, actor="coordinator"):
        with self.targets.lock:
            target = self.targets.get(tid)
            result = self._run(target, 'connection_test')
            with self.store.connect() as db:
                self.targets._audit(db, tid, target['revision'], actor, 'target.test', result)
            self._record({'id': 'target:' + tid, 'project_id': None, 'project_name': target['name'],
                          'workbench_path': '/settings/runtime', 'status': 'remote_test', 'source': {}}, result)
            return result

    def execute(self, rid, target_id, verb, *, lines=100):
        if verb not in VERBS or type(lines) is not int or not 1 <= lines <= 500 or (verb != 'fetch_log' and lines != 100):
            raise ValueError('动作或参数无效；日志行数必须为 1 至 500')
        with self.targets.lock:
            run = self.store.get(rid)
            source = run.get('source') or {}
            if run['status'] in ('cancelled', 'discarded'):
                raise Conflict('运行已终止')
            if source.get('type') == 'inspection' and not source.get('remote_read_only'):
                raise Conflict('此巡检未启用远程只读探测')
            if source.get('operation') != 'release' and source.get('type') != 'inspection':
                raise Conflict('仅部署准备或巡检运行可使用远程动作')
            if target_id not in self.targets.bindings(run['project_id'])['targets']:
                raise Conflict('目标未绑定到此项目')
            target = self.targets.get(target_id)
            if not any(t['id'] == target_id and t['revision'] == target['revision'] for t in source.get('remote_targets', [])):
                raise Conflict('目标配置与提交时不同，请重新发起运行')
            if verb not in READ_ONLY:
                if (source.get('operation') != 'release' or source.get('execute_deploy') is not True
                        or source.get('type') == 'inspection'
                        or (run.get('artifacts', {}).get('verification') or {}).get('verdict') != 'pass'
                        or run['status'] not in ('ready_for_review', 'published')):
                    raise Conflict('远程部署须为显式授权部署的 release 运行，且独立验收已通过')
                # Claim before I/O. A coordinator crash must never replay a write script.
                pending = {'target_id': target_id, 'target': target['name'], 'verb': verb, 'status': 'unverified',
                           'exit_code': None, 'executed': False, 'duration_s': 0,
                           'reason': '动作已登记但未收到完成回执；请人工核对，不自动重放'}
                cached = None
                with self.store.connect() as db:
                    inserted = db.execute('INSERT OR IGNORE INTO remote_invocations VALUES(?,?,?,?)',
                                          (rid, target_id, verb, json.dumps(pending))).rowcount
                    if not inserted:
                        cached = json.loads(db.execute('SELECT result FROM remote_invocations WHERE run_id=? AND target_id=? AND verb=?',
                                                     (rid, target_id, verb)).fetchone()['result'])
                if cached is not None:
                    self._record(run, {**cached, 'reused': True})
                    return cached
            result = self._run(target, verb, lines)
            if verb not in READ_ONLY:
                with self.store.connect() as db:
                    db.execute('UPDATE remote_invocations SET result=? WHERE run_id=? AND target_id=? AND verb=?',
                               (json.dumps(result), rid, target_id, verb))
            self._record(run, result)
            return result

    def evidence(self, run):
        # Durable write receipts remain visible even if the coordinator died before
        # updating the run's aggregate artifacts. Never resume the external write.
        with self.store.connect() as db:
            writes = [json.loads(r['result']) for r in db.execute(
                'SELECT result FROM remote_invocations WHERE run_id=?', (run['id'],))]
        if not writes:
            return run
        artifacts = dict(run.get('artifacts') or {})
        keys = {(r['target_id'], r['verb']) for r in writes}
        artifacts['remote_results'] = [r for r in artifacts.get('remote_results', [])
            if (r['target_id'], r['verb']) not in keys] + writes
        return {**run, 'artifacts': artifacts}

    def collect(self, rid, artifacts, *, inspection=False):
        run = self.store.get(rid)
        source = run.get('source') or {}
        results = []
        for target in source.get('remote_targets', []):
            verbs = ['health_check', 'service_status']
            if not inspection and source.get('execute_deploy') is True:
                verbs = ['deploy', 'health_check', 'service_status']
            for verb in verbs:
                try:
                    result = self.execute(rid, target['id'], verb)
                except (Conflict, KeyError) as exc:
                    result = {'target_id': target['id'], 'target': target['name'], 'verb': verb, 'status': 'unverified',
                              'reason': redact_text(str(exc)), 'executed': False, 'exit_code': None, 'duration_s': 0}
                    self._record(run, result)
                results.append(result)
        requested = (artifacts.get('verification') or {}).get('remote_requests')
        if not inspection and isinstance(requested, list):
            for item in requested[:8]:
                if not isinstance(item, dict) or set(item) - {'target_id', 'verb', 'lines'}:
                    continue
                if any(r['target_id'] == item.get('target_id') and r['verb'] == item.get('verb') for r in results):
                    continue
                try:
                    results.append(self.execute(rid, item.get('target_id'), item.get('verb'), lines=item.get('lines', 100)))
                except (ValueError, KeyError, TypeError):
                    # A model request cannot turn a denied verb into a global run failure.
                    continue
        artifacts['remote_results'] = results
        return results
