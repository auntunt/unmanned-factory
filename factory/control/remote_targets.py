"""Coordinator-only, pinned SSH verbs; no credentials or command text cross into agents."""
import hashlib
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

#: The one line shape a target may use to state what it did with an action, and
#: the only thing that can settle an unknown write. Carried on the target's own
#: stdout, prefixed so it is distinguishable from arbitrary service output.
RECEIPT_MARKER = 'WEBUDDY-RECEIPT '
RECEIPT_SCHEMA = 'webuddy.action.receipt/1'
#: States a receipt may declare. Only `applied` is success; the other two are
#: answers, not confirmations. An undeclared or unlisted state is not a state.
RECEIPT_STATES = ('applied', 'failed', 'unknown')

#: Environment the coordinator prefixes onto a registered command so the target
#: learns which action it is performing, or being asked about. A target that
#: ignores them behaves exactly as before and stays unverifiable, which is the
#: correct outcome rather than a failure.
ACTION_ENV = 'WEBUDDY_ACTION_ID'
INTENT_ENV = 'WEBUDDY_INTENT_DIGEST'
QUERY_ENV = 'WEBUDDY_QUERY_ACTION_ID'
#: A receipt is only accepted when it names the target and verb this side is
#: asking about, so the convention has to hand the target both. Without them a
#: cooperating target could not produce a bindable receipt at all -- it does not
#: otherwise know the coordinator's id for it.
TARGET_ENV = 'WEBUDDY_TARGET_ID'
VERB_ENV = 'WEBUDDY_VERB'


def intent_digest(intent):
    """A stable digest of the frozen intent, for the target to echo back.

    Binds a receipt to the parameters the action was claimed under. A claim that
    names this action id but a different frozen intent is a different action.
    """
    return hashlib.sha256(
        json.dumps(intent, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def receipts_in(text, *, action_id, target_id, verb, digest):
    """Every well-formed receipt in this output that is about this exact action.

    Returns (matching, malformed). Matching means: our schema, our action id, our
    target, our verb, our frozen intent digest, and a declared state from the
    closed list. Nothing here interprets prose -- a line either parses into that
    shape or it is not a receipt, so no vocabulary of denials is involved and a
    target cannot state success by accident.
    """
    matching, malformed = [], 0
    for line in (text or '').splitlines():
        stripped = line.strip()
        if not stripped.startswith(RECEIPT_MARKER):
            continue
        try:
            payload = json.loads(stripped[len(RECEIPT_MARKER):])
        except ValueError:
            malformed += 1
            continue
        if not isinstance(payload, dict) or payload.get('schema') != RECEIPT_SCHEMA:
            malformed += 1
            continue
        if payload.get('action_id') != action_id:
            # A receipt about another action is not malformed, just not ours.
            continue
        if (payload.get('target_id') != target_id or payload.get('verb') != verb
                or payload.get('intent_digest') != digest
                or payload.get('state') not in RECEIPT_STATES):
            malformed += 1
            continue
        matching.append(payload)
    return matching, malformed


def action_id(rid, target_id, verb):
    """A stable name for one external write, derived from what identifies it.

    Derived rather than random so that reopening the database, or asking a second
    time after a lost response, arrives at the same name for the same action --
    which is what makes a later reconciliation able to ask about *this* action
    instead of guessing from a timestamp.
    """
    digest = hashlib.sha256('\x00'.join((str(rid), str(target_id), str(verb))).encode())
    return 'act_' + digest.hexdigest()[:24]


def intent_of(target, verb, *, lines=100):
    """What was claimed before the I/O: enough to tell two calls apart."""
    return {'target_id': target['id'], 'target': target['name'],
            'target_revision': target.get('revision'), 'verb': verb,
            'host': target['host'], 'port': target['port'], 'user': target['user'],
            **({'lines': lines} if verb == 'fetch_log' else {})}


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

    def _run(self, target, verb, lines=100, *, identity=None):
        """Run one registered command. `identity` tells the target which action it is.

        The registered command text is never rewritten: the identity is passed as
        a leading environment assignment, so a target script that does not look
        for it runs byte-identically to before and simply cannot issue a receipt.
        That is the compatible half of the convention -- unsupported targets stay
        `unknown` instead of failing.
        """
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
        if identity:
            command = self._with_identity(command, identity)
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

    @staticmethod
    def _with_identity(command, identity):
        """Prefix the identity as shell environment assignments, quoted.

        Values are derived here (a hex action id, a hex digest), never taken from
        a caller's text, and each is `shlex.quote`d anyway so nothing in them can
        reach the target as syntax.
        """
        assignments = []
        for key, value in (
                (ACTION_ENV, identity.get('action_id')),
                (INTENT_ENV, identity.get('intent_digest')),
                (QUERY_ENV, identity.get('query_action_id')),
                (TARGET_ENV, identity.get('target_id')),
                (VERB_ENV, identity.get('verb'))):
            if value:
                assignments.append(f'{key}={shlex.quote(str(value))}')
        if not assignments:
            return command
        return ' '.join(assignments) + ' ' + command

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
                aid = action_id(rid, target_id, verb)
                intent = intent_of(target, verb, lines=lines)
                pending = {'action_id': aid, 'intent': intent,
                           'target_id': target_id, 'target': target['name'], 'verb': verb,
                           'status': 'unverified', 'exit_code': None, 'executed': False,
                           'duration_s': 0,
                           'reason': '动作已登记但未收到完成回执；请人工核对，不自动重放'}
                cached = None
                with self.store.connect() as db:
                    inserted = db.execute(
                        'INSERT OR IGNORE INTO remote_invocations'
                        '(run_id,target_id,verb,result,action_id,intent,at) VALUES(?,?,?,?,?,?,?)',
                        (rid, target_id, verb, json.dumps(pending), aid,
                         json.dumps(intent, sort_keys=True), now())).rowcount
                    if not inserted:
                        row = db.execute('SELECT result,intent FROM remote_invocations WHERE run_id=? AND target_id=? AND verb=?',
                                         (rid, target_id, verb)).fetchone()
                        cached = json.loads(row['result'])
                        claimed = row['intent']
                        # Same key, different parameters is a different action wearing
                        # this one's name. Returning the old receipt would report the
                        # earlier action's outcome for it; running it would be a second
                        # external write. Neither -- refuse.
                        if claimed and claimed != json.dumps(intent, sort_keys=True):
                            raise Conflict('同一动作键已登记过不同参数，拒绝复用回执或重发',
                                           error_type='remote_action_conflict')
                if cached is not None:
                    self._record(run, {**cached, 'reused': True})
                    return cached
            if verb in READ_ONLY:
                result = self._run(target, verb, lines)
            else:
                # The write carries its own identity, so the target can record
                # what it applied and answer for it later. Without this the only
                # thing that could ever name the action was the coordinator, and
                # a lost response left nothing to ask about.
                claimed_intent = intent_of(target, verb, lines=lines)
                digest = intent_digest(claimed_intent)
                result = self._run(target, verb, lines, identity={
                    'action_id': aid, 'intent_digest': digest,
                    'target_id': target_id, 'verb': verb})
                declared, malformed = receipts_in(result.get('summary'),
                    action_id=aid, target_id=target_id, verb=verb, digest=digest)
                receipt = self._settle_receipt(declared, malformed)
                result = {'action_id': aid, 'intent': claimed_intent,
                          'intent_digest': digest, **result,
                          **({'receipt': receipt} if receipt else {})}
                if receipt and receipt['state'] != 'applied':
                    # The target answered, and its answer is not success.
                    result.update(status='unverified', reconciled='target_reported_not_applied',
                                  reason='目标回执声明此动作未成功应用，维持未知待人工核对')
                with self.store.connect() as db:
                    db.execute('UPDATE remote_invocations SET result=? WHERE run_id=? AND target_id=? AND verb=?',
                               (json.dumps(result), rid, target_id, verb))
            self._record(run, result)
            return result

    @staticmethod
    def _frozen_intent_mismatch(run, target, claimed, verb):
        """Why this target is no longer the one the action was claimed against, or None.

        Checked against two frozen records, not against whatever is current: the
        intent stored with the claim, and the target snapshot the run was
        submitted with. Either moving means a receipt from this host would be
        about a different machine or a different configuration.
        """
        if not isinstance(claimed, dict):
            return '登记时的参数缺失，无法核对'
        for key in ('host', 'port', 'user', 'target_id'):
            expected = claimed.get(key)
            current = target['id'] if key == 'target_id' else target.get(key)
            if expected != current:
                return f'{key} 已变化'
        if claimed.get('target_revision') != target.get('revision'):
            return 'target_revision 已变化'
        if claimed.get('verb') != verb:
            return 'verb 与登记不一致'
        snapshot = (run.get('source') or {}).get('remote_targets') or []
        if not any(item.get('id') == target['id']
                   and item.get('revision') == target.get('revision') for item in snapshot):
            return '运行提交时的目标快照与当前配置不同'
        return None

    @staticmethod
    def _settle_receipt(declared, malformed):
        """One receipt, or none. Contradiction and malformation are never success.

        Two receipts for the same action that disagree mean the target cannot be
        taken at its word here, and a line that claims our schema but does not
        parse into it is a broken answer rather than an absent one; both keep the
        action unknown. A single well-formed receipt is returned as-is -- its
        declared state, not our reading of it, decides.
        """
        states = {item['state'] for item in declared}
        if malformed or len(states) > 1:
            return {'state': 'unknown', 'conflict': True,
                    'declared': sorted(states), 'malformed': malformed}
        if not declared:
            return None
        return declared[0]

    def actions(self, rid=None, *, action_id=None):
        """Read-only: what external writes were claimed, and what came back.

        Performs no I/O and no writes, so it is safe to call while an action's
        outcome is unknown -- which is exactly when someone needs to look.
        """
        query = ('SELECT run_id,target_id,verb,result,action_id,intent,at '
                 'FROM remote_invocations')
        clauses, params = [], []
        if rid is not None:
            clauses.append('run_id=?'); params.append(rid)
        if action_id is not None:
            clauses.append('action_id=?'); params.append(action_id)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        with self.store.connect() as db:
            rows = db.execute(query + ' ORDER BY at, verb', params).fetchall()
        out = []
        for row in rows:
            result = json.loads(row['result'])
            out.append({'action_id': row['action_id'] or result.get('action_id'),
                        'run_id': row['run_id'], 'target_id': row['target_id'],
                        'verb': row['verb'], 'claimed_at': row['at'],
                        'intent': json.loads(row['intent']) if row['intent'] else result.get('intent'),
                        'status': result.get('status', 'unverified'),
                        'reconciled': result.get('reconciled'),
                        'result': result})
        return out

    def reconcile(self, rid, target_id, verb):
        """Decide an unknown write from target-verifiable evidence. Never re-writes.

        Only a read-only verb runs here, and only evidence that names *this* action
        can settle it: the deployed version or action id the target reports back. A
        health check returning 200 says something is up; it does not say this
        deploy is what put it there, and treating it as proof is how a lost
        response becomes a false "published".
        """
        with self.targets.lock:
            run = self.store.get(rid)
            with self.store.connect() as db:
                row = db.execute('SELECT result,intent,action_id FROM remote_invocations '
                                 'WHERE run_id=? AND target_id=? AND verb=?',
                                 (rid, target_id, verb)).fetchone()
            if row is None:
                raise Conflict('没有这条外部动作的登记记录')
            record = json.loads(row['result'])
            aid = row['action_id'] or record.get('action_id')
            if record.get('status') == 'pass':
                return {**record, 'reconciled': record.get('reconciled', 'already_known')}
            target = self.targets.get(target_id)
            claimed = json.loads(row['intent']) if row['intent'] else record.get('intent')
            # Before asking anyone: is this still the machine the action was
            # claimed against? A same-numbered claim collected from a host that
            # has since been re-pointed would be a receipt about somewhere else.
            frozen = self._frozen_intent_mismatch(run, target, claimed, verb)
            if frozen is not None:
                settled = {**record, 'status': 'unverified',
                           'reconciled': 'frozen_target_changed', 'reconciled_at': now(),
                           'reason': '目标配置与登记该动作时不同，拒绝据此认领同号回执：' + frozen,
                           'reconcile_evidence': ''}
                with self.store.connect() as db:
                    db.execute('UPDATE remote_invocations SET result=? WHERE run_id=? AND target_id=? AND verb=?',
                               (json.dumps(settled), rid, target_id, verb))
                self._record(run, {**settled, 'verb': verb, 'target_id': target_id,
                                   'target': target['name']})
                return settled
            digest = record.get('intent_digest') or intent_digest(claimed)
            # Read-only, and it names the action being asked about rather than
            # asking "is anything healthy".
            probe = self._run(target, 'service_status', identity={
                'query_action_id': aid, 'intent_digest': digest,
                'target_id': target_id, 'verb': verb} if aid else None)
            evidence = probe.get('summary') or ''
            declared, malformed = receipts_in(evidence, action_id=aid, target_id=target_id,
                                              verb=verb, digest=digest)
            receipt = self._settle_receipt(declared, malformed) if aid else None
            if probe['status'] == 'pass' and receipt and receipt.get('state') == 'applied':
                settled = {**record, 'status': 'pass', 'reconciled': 'target_reported_action',
                           'reconciled_at': now(), 'receipt': receipt,
                           'reason': '目标按约定回执声明已应用此动作，且绑定动作标识与冻结意图',
                           'reconcile_evidence': evidence[:2000]}
            else:
                reason = ('目标无法按约定回执声明此动作，维持未知待人工核对；'
                          '不因健康检查通过或输出中出现动作标识就认定本次写入成功')
                if receipt and receipt.get('state') != 'applied':
                    reason = '目标回执声明此动作并未成功应用（' + str(receipt.get('state')) + '），维持未知待人工核对'
                settled = {**record, 'status': 'unverified',
                           'reconciled': 'unknown_target_cannot_confirm',
                           'reconciled_at': now(),
                           **({'receipt': receipt} if receipt else {}),
                           'reason': reason,
                           'reconcile_evidence': evidence[:2000]}
            with self.store.connect() as db:
                db.execute('UPDATE remote_invocations SET result=? WHERE run_id=? AND target_id=? AND verb=?',
                           (json.dumps(settled), rid, target_id, verb))
            self._record(run, {**settled, 'verb': verb, 'target_id': target_id,
                               'target': target['name']})
            return settled

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
