"""Administrator-owned target registry, per-target keys and audited project bindings."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import urlparse
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

from factory.control.store import Conflict, now, scrub

VERBS = ('health_check', 'service_status', 'fetch_log', 'deploy', 'rollback')


def fingerprint(public_key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(public_key.split()[1], validate=True)).digest()).decode().rstrip('=')


class TargetStore:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS deploy_targets(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_deploy_targets(project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, targets TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deploy_target_audit(id INTEGER PRIMARY KEY, target_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS remote_invocations(run_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    verb TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(run_id,target_id,verb));''')
            # The row already identified the action by (run, target, verb) and
            # already survived a lost response. What it could not say was *which*
            # action it was, from outside this row: the parameters it was claimed
            # with, and a stable id a later reconciliation can ask the target
            # about. Added, not replaced -- the existing primary key still decides
            # identity, so a repeat still returns the original receipt.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(remote_invocations)')}
            for name in ('action_id', 'intent', 'at'):
                if name not in columns:
                    db.execute(f'ALTER TABLE remote_invocations ADD COLUMN {name} TEXT')

    def key_dir(self):
        raw = os.environ.get('FACTORY_DEPLOY_KEY_DIR')
        if not raw or not Path(raw).is_absolute():
            raise Conflict('管理员需先配置独立的 FACTORY_DEPLOY_KEY_DIR 目录')
        path = Path(raw)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise Conflict('部署密钥目录不能使用符号链接')
        path = path.resolve()
        data = Path(self.store.path).resolve().parent
        if path == Path('/') or path == Path.home() or path.is_relative_to(data) or data.is_relative_to(path):
            raise Conflict('部署密钥目录必须位于数据目录之外')
        with self.store.connect() as db:
            for row in db.execute('SELECT data FROM projects'):
                workspace = Path(json.loads(row['data'])['workspace']).resolve()
                if path.is_relative_to(workspace) or workspace.is_relative_to(path):
                    raise Conflict('部署密钥目录不能与项目工作区重叠')
        root = getattr(self, 'workspace_root', None)
        if root and (path.is_relative_to(root) or Path(root).is_relative_to(path)):
            raise Conflict('部署密钥目录须与整个工作区根目录隔离')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.stat().st_uid != os.getuid():
            raise Conflict('部署密钥目录须归服务进程所有')
        if any(not re.fullmatch(r'[a-f0-9]{32}|known-[A-Za-z0-9_-]+', item.name) for item in path.iterdir()):
            raise Conflict('部署密钥目录须专用，不能包含其他文件')
        path.chmod(0o700)
        return path

    def key_path(self, tid):
        if not re.fullmatch('[a-f0-9]{32}', tid):
            raise KeyError(tid)
        path = self.key_dir() / tid
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise Conflict('部署密钥文件缺失或权限不符合要求')
        return path

    def list(self):
        with self.store.connect() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM deploy_targets ORDER BY rowid')]

    def get(self, tid):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM deploy_targets WHERE id=?', (tid,)).fetchone()
        if not row:
            raise KeyError(tid)
        return json.loads(row['data'])

    @staticmethod
    def _validate_service_url(url):
        """校验 service_url：必须是 http 或 https 绝对地址。"""
        if not url:
            return ''
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            raise ValueError('可展示地址必须使用 http 或 https 协议')
        if not parsed.netloc:
            raise ValueError('可展示地址必须是完整的绝对 URL')
        return url

    @staticmethod
    def validate(values):
        required = {'name', 'host', 'port', 'user', 'host_fingerprint', 'commands'}
        optional = {'service_url'}
        if not required <= set(values) or set(values) - required - optional:
            raise ValueError('目标字段不完整')
        if not isinstance(values['name'], str) or not 1 <= len(values['name'].strip()) <= 120:
            raise ValueError('请填写目标名称')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', values['host']):
            raise ValueError('主机只能是域名或 IP 地址')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', values['user']):
            raise ValueError('SSH 用户名无效')
        if type(values['port']) is not int or not 1 <= values['port'] <= 65535:
            raise ValueError('端口无效')
        if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', values['host_fingerprint']):
            raise ValueError('请提供 SHA256 主机公钥指纹')
        commands = values['commands']
        if not isinstance(commands, dict) or set(commands) - set(VERBS):
            raise ValueError('仅支持五个预注册动作')
        if any(not isinstance(v, str) or len(v) > 4000 or '\x00' in v or '\n' in v for v in commands.values()):
            raise ValueError('动作必须是单行固定命令、URL 或日志路径')
        if any(scrub(v) != v for v in commands.values()):
            raise ValueError('预注册动作不得包含明文凭据，请在目标服务器脚本内配置')
        if commands.get('fetch_log') and not commands['fetch_log'].startswith('/'):
            raise ValueError('fetch_log 必须是绝对日志路径')
        values['service_url'] = TargetStore._validate_service_url(values.get('service_url', ''))
        return values

    def _audit(self, db, tid, revision, actor, action, data):
        db.execute('INSERT INTO deploy_target_audit(target_id,revision,actor,action,data,at) VALUES(?,?,?,?,?,?)',
                   (tid, revision, str(actor), action, json.dumps(scrub(data), ensure_ascii=False), now()))

    def create(self, values, actor):
        self.validate(values)
        with self.lock:
            tid = uuid.uuid4().hex
            key = Ed25519PrivateKey.generate()
            public = key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
            path = self.key_dir() / tid
            data = {**values, 'id': tid, 'public_key': public, 'public_fingerprint': fingerprint(public),
                    'revision': 1, 'created_by': actor, 'created_at': now()}
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()))
                    output.flush(); os.fsync(output.fileno())
                with self.store.connect() as db:
                    db.execute('INSERT INTO deploy_targets VALUES(?,?)', (tid, json.dumps(data)))
                    self._audit(db, tid, 1, actor, 'target.create', data)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return data

    def update(self, tid, values, revision, actor):
        self.validate(values)
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get(tid)
            if old['revision'] != revision:
                raise Conflict('目标配置已变化，请刷新')
            data = {**old, **values, 'revision': revision + 1}
            db.execute('UPDATE deploy_targets SET data=? WHERE id=?', (json.dumps(data), tid))
            self._audit(db, tid, revision + 1, actor, 'target.update', data)
        return data

    def delete(self, tid, revision, actor):
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get(tid)
            if old['revision'] != revision:
                raise Conflict('目标配置已变化，请刷新')
            if any(tid in json.loads(r['targets']) for r in db.execute('SELECT targets FROM project_deploy_targets')):
                raise Conflict('请先解除项目绑定再删除目标')
            path = self.key_dir() / tid
            if path.exists() or path.is_symlink():
                self.key_path(tid).unlink()
            db.execute('DELETE FROM deploy_targets WHERE id=?', (tid,))
            self._audit(db, tid, revision + 1, actor, 'target.delete', {'name': old['name']})

    def bindings(self, pid):
        self.store.project(pid)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM project_deploy_targets WHERE project_id=?', (pid,)).fetchone()
        return {'revision': row['revision'] if row else 0, 'targets': json.loads(row['targets']) if row else []}

    def bind(self, pid, tids, revision, actor):
        self.store.project(pid)
        if len(tids) > 8 or len(tids) != len(set(tids)):
            raise ValueError('最多绑定八个不重复目标')
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for tid in tids:
                self.get(tid)
            if self.bindings(pid)['revision'] != revision:
                raise Conflict('项目目标绑定已变化，请刷新')
            db.execute('INSERT INTO project_deploy_targets VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,targets=excluded.targets',
                       (pid, revision + 1, json.dumps(tids)))
            db.execute('INSERT INTO project_settings_audit(project_id,revision,actor,action,data,at) VALUES(?,?,?,?,?,?)',
                       (pid, revision + 1, str(actor), 'target.bind', json.dumps(tids), now()))
        return self.bindings(pid)

    def last_checks(self):
        """Most recent connection test result per target, from the audit trail."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT target_id, data, at FROM deploy_target_audit "
                "WHERE action='target.test' AND id IN "
                "(SELECT MAX(id) FROM deploy_target_audit WHERE action='target.test' GROUP BY target_id)"
            ).fetchall()
        from factory.control.evidence_identity import HEALTH_TTL_S, health_is_current
        results = {}
        for row in rows:
            try:
                data = json.loads(row['data'])
            except (json.JSONDecodeError, TypeError):
                continue
            # An observation of reachability is about the moment it was made. This
            # surface is the one place a recorded `pass` is read back as the
            # target's state, so it carries whether that reading is still current
            # instead of presenting last week's probe as today's answer.
            results[row['target_id']] = {
                'status': data.get('status', 'unverified'),
                'reason': data.get('reason', ''),
                'checked_at': row['at'],
                'current': health_is_current(row['at']),
                'ttl_s': HEALTH_TTL_S,
            }
        return results

    def snapshot(self, pid):
        result = []
        for tid in self.bindings(pid)['targets']:
            target = self.get(tid)
            entry = {'id': tid, 'name': target['name'], 'revision': target['revision']}
            if target.get('service_url'):
                entry['service_url'] = target['service_url']
            else:
                entry['service_url'] = ''
            result.append(entry)
        return result
