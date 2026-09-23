"""webuddy 运维维护子系统的共享业务核心（契约 ``maintenance-subsystem/1``）。

HTTP、CLI 和页面操作的是这里的同一组对象。这个模块只拥有既有表里没有的东西：

* 代码库的**探测结果**（``maintenance_repo_probes``）——项目本身仍是既有 ``projects``
  表里的那一行，登记同一仓库复用既有 ``project_id``；
* **需求**（``maintenance_requirements``）——人工与机器提交共用 ``Intake.submit``，
  需求派发后变成既有的维护任务（``issue_maintenance``），状态从执行派生，不在这里另存；
* **接入来源**（``maintenance_intake_sources``）——机器提交的令牌与项目范围，库内只存哈希。

监控总览与关系画布都是对上面这些与既有 ``runs``/``events``/``control_jobs`` 的只读投影，
节点位置、计数都不持有业务状态。没有数据源的指标（CPU/内存、应用探针、告警）如实报
``not_connected``，不从任务成功推断服务器健康。
"""
from __future__ import annotations

import base64
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from factory.control.store import Conflict, now
from factory.harness.checkenv import check_env

VERSION = '0.1.0'
CONTRACT_VERSION = 'maintenance-subsystem/1'
PLUGIN_ID = 'issue-maintenance'

REPO_STATE_LABEL = {'pending': '待分析', 'analyzing': '分析中', 'needs_input': '待补充',
                    'ready': '可开始维护', 'failed': '接入失败'}
REQUIREMENT_STATUS_LABEL = {'received': '已接收', 'dispatched': '已派发执行',
                            'pending_dispatch': '已接收，待派发', 'dispatch_failed': '派发失败'}
SOURCE_KINDS = ('manual', 'api', 'cli')

#: Run states grouped the way the monitor counts them. Anything not listed is not
#: silently bucketed: it shows up as ``other`` and is counted nowhere.
_ACTIVE = {'received', 'queued', 'planning', 'running', 'verifying', 'publishing',
           'requirement_analysis'}
_ANSWER = {'needs_clarification'}
_APPROVAL = {'awaiting_approval', 'awaiting_spec_confirmation'}
_BLOCKED = {'needs_human', 'failed'}
_DELIVERED = {'ready_for_review', 'published'}

_EVENT_LABELS = {
    'user.message': '收到需求', 'queue.enqueued': '进入执行队列', 'run.planning': '开始制定计划',
    'run.started': '开始修改', 'run.verified': '检查完成，产物就绪', 'run.failed': '执行失败',
    'run.blocked': '执行受阻', 'run.recovered': '服务重启后恢复', 'run.resumed': '继续执行',
    'clarification.requested': '模型提问，等待业务回答', 'clarification.answered': '已回答问题',
    'approval.requested': '计划待批准', 'run.approved': '计划已批准', 'run.cancelled': '已取消',
    'budget.exhausted': '预算用尽', 'followup.pending': '收到补充信息', 'check.completed': '检查完成',
    'maintenance.memory_scope_recorded': '记录计划涉及的项目记忆',
    'execution.mode_selected': '选定执行方式', 'modules.frozen': '固定本次能力模块',
    'policy.frozen': '固定本次执行策略', 'runtime.configuration_frozen': '固定本次模型配置',
    'assistant.message': '执行器汇报进展', 'task.started': '子任务开始', 'task.completed': '子任务完成',
    'verification.completed': '独立验证完成', 'run.cancel_requested': '请求取消',
    'plan.created': '计划已生成', 'triage.decided': '判定是否需要人工批准', 'check.result': '项目检查出结果',
    'git.commit': '提交修改',
}

#: What the monitor's event feed shows. Everything else is engine detail.
_FEED_KINDS = frozenset({
    'user.message', 'run.planning', 'plan.created', 'triage.decided', 'approval.requested',
    'run.approved', 'run.started', 'task.completed', 'check.result', 'run.verified', 'run.failed',
    'run.blocked', 'run.recovered', 'run.resumed', 'clarification.requested',
    'clarification.answered', 'budget.exhausted', 'followup.pending', 'run.cancelled',
    'run.cancel_requested', 'git.commit'})

_URL = re.compile(r'^(?:https?://|ssh://|git@)[^\s]+$')
_SLUG = re.compile(r'[^A-Za-z0-9_.-]+')


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _git(args, cwd, timeout=15):
    try:
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ok(done) -> bool:
    return done is not None and done.returncode == 0


def local_timezone() -> str:
    """An IANA name for the server's day window, or the abbreviation if none is known."""
    explicit = os.getenv('FACTORY_TIMEZONE') or os.getenv('TZ')
    if explicit:
        return explicit
    try:
        target = os.readlink('/etc/localtime')
        if 'zoneinfo/' in target:
            return target.split('zoneinfo/', 1)[1]
    except OSError:
        pass
    return _dt.datetime.now().astimezone().tzname() or 'local'


def _day_window():
    start = _dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + _dt.timedelta(days=1)
    return start, end


def _parse(ts):
    if not ts:
        return None
    try:
        value = _dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)


# Credentials a registration may name. Only names the platform actually holds;
# anything else is refused rather than stored and silently ignored.
PLATFORM_CREDENTIALS = {'github': '平台 GitHub 凭据（服务端 FACTORY_GITHUB_TOKEN）'}
_GITHUB_HTTPS = re.compile(r'^https://github\.com/[^/\s@]+/[^/\s@]+?(?:\.git)?/?$', re.I)
_AUTH_MARKERS = ('could not read username', 'terminal prompts disabled', 'authentication failed',
                 'invalid username or password', 'repository not found', 'permission denied (publickey)',
                 'requested url returned error: 403', 'requested url returned error: 401')
_NETWORK_MARKERS = ('could not resolve host', 'failed to connect', 'connection timed out',
                    'network is unreachable', 'connection refused')


def _redact_git_output(text, hidden=()):
    text = str(text or '')
    for secret in hidden:
        if secret:
            text = text.replace(secret, '***')
    text = re.sub(r'(?i)(authorization:\s*\w+\s+)\S+', r'\1***', text)
    return re.sub(r'//[^/@\s]+@', '//***@', text)


def _clone_failure(stderr, *, credential=None, github=False, timed_out=False, hidden=()):
    """A failed clone as (reason, message, next step, redacted detail)."""
    detail = _redact_git_output(stderr, hidden).strip()[-300:]
    text = detail.lower()
    if timed_out:
        return {'reason': 'timeout', 'message': '克隆超时',
                'next_step': '检查执行主机到代码托管的网络，然后点“重新接入”', 'detail': detail}
    if any(m in text for m in _AUTH_MARKERS):
        if credential:
            return {'reason': 'auth', 'message': f'{PLATFORM_CREDENTIALS[credential]}无权访问该仓库或已失效',
                    'next_step': '确认平台 GitHub 账号能访问该仓库（私有仓库需被加入协作者或获得组织授权），然后点“重新接入”',
                    'detail': detail}
        return {'reason': 'auth', 'message': '执行主机没有访问该仓库的凭据',
                'next_step': ('管理员在服务端配置 FACTORY_GITHUB_TOKEN 后点“重新接入”' if github else
                              '在执行主机上为该代码托管配置 SSH 或凭据后点“重新接入”，或改用本地目录登记'),
                'detail': detail}
    if 'remote branch' in text and 'not found' in text:
        return {'reason': 'branch', 'message': '指定的分支在远端不存在',
                'next_step': '用正确的分支名重新登记该仓库', 'detail': detail}
    if any(m in text for m in _NETWORK_MARKERS):
        return {'reason': 'network', 'message': '执行主机无法连接代码托管',
                'next_step': '检查执行主机网络或代理后点“重新接入”', 'detail': detail}
    return {'reason': 'unknown', 'message': '克隆失败', 'next_step': '查看详情，修正后点“重新接入”', 'detail': detail}


def _repository_from_url(url: str) -> str:
    """``owner/name`` from a git URL, in the shape the projects table requires."""
    path = re.sub(r'^(?:[a-z+]+://[^/]+/|git@[^:]+:)', '', url.strip()).rstrip('/')
    path = re.sub(r'\.git$', '', path)
    parts = [p for p in path.split('/') if p]
    if len(parts) < 2:
        raise ValueError('仓库地址需要包含所有者和仓库名，例如 https://host/owner/name.git')
    owner, name = _SLUG.sub('-', parts[-2]).strip('-.'), _SLUG.sub('-', parts[-1]).strip('-.')
    if not owner or not name:
        raise ValueError('无法从仓库地址解析出所有者和仓库名')
    return f'{owner}/{name}'


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def _ensure_tables(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS maintenance_repo_probes(
            project_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS maintenance_requirements(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, idem_key TEXT UNIQUE,
            received_at TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS maintenance_requirements_project
            ON maintenance_requirements(project_id, received_at);
        CREATE TABLE IF NOT EXISTS maintenance_intake_sources(
            id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
        ''')


# ---------------------------------------------------------------------------
# Repository detection
# ---------------------------------------------------------------------------
def detect_stack(root: Path) -> tuple[list[dict], list[dict]]:
    """What the files say, not what has run: every result here is ``found``."""
    stack, checks = [], []

    def check(name, argv, evidence):
        checks.append({'name': name, 'argv': argv, 'evidence': evidence})

    package = root / 'package.json'
    if package.is_file():
        stack.append({'name': 'Node.js', 'evidence': 'package.json'})
        try:
            scripts = json.loads(package.read_text(encoding='utf-8')).get('scripts') or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        runner = 'pnpm' if (root / 'pnpm-lock.yaml').is_file() else (
            'yarn' if (root / 'yarn.lock').is_file() else 'npm')
        for script in ('test', 'build', 'lint'):
            if isinstance(scripts, dict) and scripts.get(script):
                check(f'{script}', [runner, 'run', script], f'package.json scripts.{script}')
    if any((root / f).is_file() for f in ('pyproject.toml', 'setup.py', 'requirements.txt')):
        marker = next(f for f in ('pyproject.toml', 'setup.py', 'requirements.txt') if (root / f).is_file())
        stack.append({'name': 'Python', 'evidence': marker})
        if (root / 'tests').is_dir() or (root / 'test').is_dir():
            uses_uv = (root / 'uv.lock').is_file()
            pytest_argv = ['uv', 'run', 'pytest', '-q'] if uses_uv else ['python3', '-m', 'pytest', '-q']
            dockerfile = 'Dockerfile.test' if (root / 'Dockerfile.test').is_file() else 'Dockerfile'
            in_docker = (root / dockerfile).is_file()
            check('pytest', ['@dockerfile', *pytest_argv] if in_docker else pytest_argv,
                  'tests/ 目录' + ('，uv.lock' if uses_uv else '') +
                  (f'，{dockerfile}（容器内执行）' if in_docker else ''))
    if (root / 'go.mod').is_file():
        stack.append({'name': 'Go', 'evidence': 'go.mod'})
        check('go-test', ['go', 'test', './...'], 'go.mod')
    if (root / 'Cargo.toml').is_file():
        stack.append({'name': 'Rust', 'evidence': 'Cargo.toml'})
        check('cargo-test', ['cargo', 'test'], 'Cargo.toml')
    if (root / 'pom.xml').is_file():
        stack.append({'name': 'Java (Maven)', 'evidence': 'pom.xml'})
        check('maven-test', ['mvn', '-q', 'test'], 'pom.xml')
    if (root / 'build.gradle').is_file() or (root / 'build.gradle.kts').is_file():
        stack.append({'name': 'Java/Kotlin (Gradle)', 'evidence': 'build.gradle'})
        if (root / 'gradlew').is_file():
            check('gradle-test', ['./gradlew', 'test'], 'gradlew')
    makefile = root / 'Makefile'
    if makefile.is_file():
        stack.append({'name': 'Make', 'evidence': 'Makefile'})
        try:
            if re.search(r'^test\s*:', makefile.read_text(encoding='utf-8', errors='replace'), re.M):
                check('make-test', ['make', 'test'], 'Makefile test 目标')
        except OSError:
            pass
    if (root / 'Dockerfile').is_file() or (root / 'Dockerfile.test').is_file():
        stack.append({'name': 'Docker 镜像', 'evidence':
                      'Dockerfile.test' if (root / 'Dockerfile.test').is_file() else 'Dockerfile'})
    return stack, checks


def _check_runnable(argv, root) -> tuple[bool, str]:
    """Whether a suggested check's execution backend can start, not whether it passes.

    Docker checks only probe the daemon; no image is built and no repository
    code is run here. Host ``python3 -m pytest`` still needs an importable pytest.
    """
    if argv and argv[0] == '@dockerfile':
        dockerfile = root / ('Dockerfile.test' if (root / 'Dockerfile.test').is_file() else 'Dockerfile')
        if not dockerfile.is_file():
            return False, '找不到 Dockerfile，不能执行容器检查'
        env = check_env()
        docker = shutil.which('docker', path=env.get('PATH', ''))
        if not docker:
            return False, '执行主机上找不到 Docker CLI；容器检查不会退回宿主机'
        try:
            done = subprocess.run([docker, 'info', '--format', '{{.ServerVersion}}'],
                                  capture_output=True, text=True, timeout=5, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return False, '无法连接 Docker daemon；容器检查不会退回宿主机'
        if done.returncode != 0:
            return False, '无法连接 Docker daemon；容器检查不会退回宿主机'
        return True, 'Docker daemon 可连接；镜像尚未构建，容器内检查尚未运行'
    exe = argv[0]
    if not (shutil.which(exe) or (root / exe).is_file()):
        return False, '执行主机上找不到该命令'
    if len(argv) >= 3 and argv[1] == '-m' and 'python' in Path(exe).name:
        try:
            done = subprocess.run([exe, '-c', f'import {argv[2]}'], capture_output=True,
                                  text=True, timeout=15, cwd=str(Path.home()))
        except (OSError, subprocess.TimeoutExpired):
            return False, f'无法确认 {exe} 能否导入 {argv[2]}'
        if done.returncode != 0:
            return False, f'执行主机的 {exe} 没有安装 {argv[2]}，采纳后检查会失败'
    return True, '命令在执行主机上可启动（尚未运行）'


# ---------------------------------------------------------------------------
# The subsystem
# ---------------------------------------------------------------------------
class MaintenanceSubsystem:
    """One object per process, handed the existing service and the gated task port.

    ``tasks`` is what ``issue_maintenance_webuddy.tasks_for`` returns -- the same
    gated port the old routes and the CLI use -- so creation, the plugin stop and
    project authorization are enforced where they already are.
    """

    def __init__(self, svc, tasks, *, workspace_root=None, identity=None):
        self.svc = svc
        self.store = svc.store
        self.tasks = tasks
        self.identity = identity
        self.workspace_root = Path(workspace_root or os.getenv('FACTORY_WORKSPACE_ROOT', '~/projects')
                                   ).expanduser().resolve()
        self._analysis = {}
        self._analysis_lock = threading.Lock()
        # Project ids with a clone in flight in this process: one clone per project.
        self._cloning = set()
        _ensure_tables(self.store)

    # -- authorization -------------------------------------------------------
    def _permitted(self, actor, project_id) -> bool:
        try:
            self.tasks.port.identity.require(actor, project_id)
            return True
        except Exception:  # noqa: BLE001 - any refusal means "not in scope"
            return False

    def _require(self, actor, project_id):
        self.store.project(project_id)
        self.tasks.port.identity.require(actor, project_id)

    # -- probes --------------------------------------------------------------
    def _probe_record(self, project_id):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM maintenance_repo_probes WHERE project_id=?',
                             (project_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save_probe(self, project_id, record):
        with self.store.connect() as db:
            db.execute('INSERT INTO maintenance_repo_probes VALUES (?,?) ON CONFLICT(project_id) '
                       'DO UPDATE SET data=excluded.data',
                       (project_id, json.dumps(record, ensure_ascii=False)))

    def maintained_project_ids(self) -> list[str]:
        """Projects this subsystem looks after: registered here, or already carrying a task."""
        from factory.control.issue_maintenance import MaintenanceStore
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute('SELECT project_id FROM maintenance_repo_probes')]
        ids += [r['project_id'] for r in MaintenanceStore(self.store).tasks()]
        known = {p['id'] for p in self.store.projects()}
        seen, ordered = set(), []
        for pid in ids:
            if pid in known and pid not in seen:
                seen.add(pid)
                ordered.append(pid)
        return ordered

    def _scope(self, actor, project_id=None) -> list[str]:
        ids = self.maintained_project_ids()
        if project_id:
            if project_id not in ids:
                self.store.project(project_id)  # 404 for an unknown id
                ids = [project_id]
            else:
                ids = [project_id]
        return [pid for pid in ids if self._permitted(actor, pid)]

    # -- repositories --------------------------------------------------------
    def register_repo(self, *, source, name, actor, branch=None, credential_ref=None,
                      background=True, synthetic=False) -> dict:
        source = str(source or '').strip()
        name = str(name or '').strip()
        if not source:
            raise ValueError('请填写代码库地址或执行主机上的目录')
        if not name or len(name) > 120:
            raise ValueError('请填写 1-120 个字符的项目名称')
        if credential_ref and not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', str(credential_ref)):
            raise ValueError('凭据引用只能是平台已配置的凭据名称，不能粘贴密钥本身')
        if credential_ref and credential_ref not in PLATFORM_CREDENTIALS:
            raise ValueError(f'凭据引用 {credential_ref} 未在平台配置；可用：' + '、'.join(PLATFORM_CREDENTIALS)
                             + '（GitHub 仓库留空即使用平台 GitHub 凭据）')
        if credential_ref == 'github' and not _GITHUB_HTTPS.match(source):
            raise ValueError('平台 GitHub 凭据只用于 https://github.com/<所有者>/<仓库> 形式的地址')
        if branch and not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', str(branch)):
            raise ValueError('分支名格式无效')
        if _URL.match(source):
            view = self._register_url(source, name, actor, branch, credential_ref, background)
        else:
            view = self._register_path(source, name, actor, branch, credential_ref)
        if synthetic and not view.get('synthetic'):
            view = {**self.set_synthetic(view['project_id'], True, actor=actor),
                    'reused': view.get('reused', False)}
        return view

    def set_synthetic(self, project_id, synthetic, *, actor) -> dict:
        """Declare (or withdraw) that this repository is a synthetic/demo one.

        Explicit, never guessed from a README. It applies to requirements received
        from now on; tasks already created keep what they were created with, so a
        past receipt is never rewritten.
        """
        self._require(actor, project_id)
        record = self._probe_record(project_id) or {'state': 'pending'}
        record['synthetic'] = bool(synthetic)
        record['synthetic_set_by'] = actor.get('username')
        record['synthetic_set_at'] = now()
        self._save_probe(project_id, record)
        return self.repo_view(project_id, actor=actor)

    def _repo_synthetic(self, project_id) -> bool:
        return bool((self._probe_record(project_id) or {}).get('synthetic'))

    def _existing(self, *, repository=None, workspace=None):
        for project in self.store.projects():
            if repository and project.get('repository', '').casefold() == repository.casefold():
                return project
            if workspace and Path(project.get('workspace', '')).expanduser().resolve() == workspace:
                return project
        return None

    def _reuse(self, project, actor, credential_ref, background=True, branch=None):
        self._require(actor, project['id'])
        record = self._probe_record(project['id']) or {'state': 'pending'}
        needs_clone = self._needs_clone(project, record)
        if branch and branch != project['base_branch']:
            if not needs_clone:
                # A working checkout is never switched behind the user's back.
                raise ValueError(f"该仓库已接入，基线分支为 {project['base_branch']}；不会自动切换到 {branch}。"
                                 '如需改用该分支，请在项目设置里修改基线分支')
        if branch and needs_clone:
            # The checkout never arrived: the corrected choice is the one to clone.
            record['requested_branch'] = branch
        if credential_ref:
            record['credential_ref'] = credential_ref
        self._save_probe(project['id'], record)
        if needs_clone:
            self._start_clone(project['id'], background=background)
        elif record.get('state') in (None, 'pending'):
            self.probe(project['id'], actor=actor)
        return {**self.repo_view(project['id'], actor=actor), 'reused': True}

    def _register_path(self, source, name, actor, branch, credential_ref):
        root = Path(source).expanduser().resolve()
        existing = self._existing(workspace=root)
        if existing:
            return self._reuse(existing, actor, credential_ref, branch=branch)
        if not root.is_relative_to(self.workspace_root) or root == self.workspace_root:
            raise ValueError(f'本地目录必须位于执行主机的工作区根目录（FACTORY_WORKSPACE_ROOT={self.workspace_root}）'
                             '下的独立子目录；网页不能读取你电脑上的文件')
        if not root.is_dir():
            raise ValueError('执行主机上不存在这个目录')
        top = _git(['rev-parse', '--show-toplevel'], root)
        if not _ok(top) or Path(top.stdout.strip()).resolve() != root:
            raise ValueError('这个目录不是 Git 仓库根目录；请先在该目录初始化并提交一个基线')
        remote = _git(['remote', 'get-url', 'origin'], root)
        repository = None
        if _ok(remote) and remote.stdout.strip():
            try:
                repository = _repository_from_url(remote.stdout.strip())
            except ValueError:
                repository = None
        repository = repository or f'local/{_SLUG.sub("-", root.name).strip("-.") or "repo"}'
        existing = self._existing(repository=repository)
        if existing:
            return self._reuse(existing, actor, credential_ref, branch=branch)
        base = branch or (_git(['symbolic-ref', '--short', 'HEAD'], root).stdout.strip()
                          if _ok(_git(['symbolic-ref', '--short', 'HEAD'], root)) else 'main')
        if not _ok(_git(['rev-parse', '--verify', f'refs/heads/{base}'], root)):
            raise ValueError(f'本地分支 {base} 不存在，请在高级配置里指定已有分支')
        project = self._add_project(name, repository, root, base, actor)
        self._save_probe(project['id'], {'state': 'pending', 'source': str(root),
                                         'credential_ref': credential_ref})
        self.probe(project['id'], actor=actor)
        return self.repo_view(project['id'], actor=actor)

    def _register_url(self, url, name, actor, branch, credential_ref, background):
        repository = _repository_from_url(url)
        existing = self._existing(repository=repository)
        if existing:
            return self._reuse(existing, actor, credential_ref, background, branch=branch)
        target = self.workspace_root / 'maintained' / repository.replace('/', '__')
        if target.exists() and any(target.iterdir()):
            raise ValueError(f'执行主机上的目标目录已存在且非空：{target}；请改用本地目录方式登记')
        target.parent.mkdir(parents=True, exist_ok=True)
        project = self._add_project(name, repository, target, branch or 'main', actor)
        self._save_probe(project['id'], {'state': 'analyzing', 'source': url,
                                         'credential_ref': credential_ref, 'started_at': now(),
                                         # None = not specified (follow the remote default); kept for every retry.
                                         'requested_branch': branch or None})

        self._start_clone(project['id'], background=background)
        return self.repo_view(project['id'], actor=actor)

    def _add_project(self, name, repository, root, base, actor):
        return self.store.add_project({
            'name': name, 'repository': repository, 'workspace': str(root),
            'base_branch': base, 'checks': {}, 'auto_issues': False, 'auto_publish': False,
            'budget_usd': None, 'budget_source': 'inherit',
            'actor': actor.get('username', 'system'), 'registered_by': 'maintenance-subsystem'})

    def _github_token(self):
        publisher = getattr(self.svc, 'publisher', None)
        return getattr(publisher, 'token', '') or os.environ.get('FACTORY_GITHUB_TOKEN', '')

    def _needs_clone(self, project, record) -> bool:
        """A URL-registered repository whose checkout never arrived.

        'analyzing' left behind by a process that died mid-clone is not a clone
        in flight; only this process's own set says one is running.
        """
        if not _URL.match(str(record.get('source') or '')):
            return False
        with self._analysis_lock:
            if project['id'] in self._cloning:
                return False
        root = Path(project['workspace']).expanduser()
        return not (root.is_dir() and _ok(_git(['rev-parse', '--git-dir'], root)))

    def _start_clone(self, project_id, *, background) -> bool:
        """Clone (again) into the project's own workspace; one clone per project at a time."""
        with self._analysis_lock:
            if project_id in self._cloning:
                return False
            self._cloning.add(project_id)
        record = self._probe_record(project_id) or {}
        record.update({'state': 'analyzing', 'started_at': now(), 'access': None})
        self._save_probe(project_id, record)

        def work():
            try:
                project = self.store.project(project_id)
                if 'requested_branch' in record:
                    requested = record['requested_branch']
                else:
                    # Registered before the choice was recorded: an explicit branch
                    # was stored as base_branch; 'main' was also the unspecified default.
                    requested = None if project['base_branch'] == 'main' else project['base_branch']
                self._clone_and_probe(project_id, record['source'], Path(project['workspace']), requested)
            finally:
                with self._analysis_lock:
                    self._cloning.discard(project_id)

        if background:
            threading.Thread(target=work, daemon=True, name=f'maintenance-clone-{project_id[:8]}').start()
        else:
            work()
        return True

    def _clone_env(self, url, credential_ref):
        """(env, credential used, secrets to redact) or a refusal dict. Never falls back silently."""
        github = bool(_GITHUB_HTTPS.match(url))
        if credential_ref and credential_ref not in PLATFORM_CREDENTIALS:
            return {'reason': 'credential_unknown', 'message': f'凭据引用 {credential_ref} 未在平台配置',
                    'next_step': '重新登记该仓库：GitHub 仓库留空凭据即使用平台 GitHub 凭据', 'detail': ''}
        if credential_ref == 'github' and not github:
            return {'reason': 'credential_host', 'message': '平台 GitHub 凭据只用于 github.com 的 HTTPS 地址',
                    'next_step': '改用 https://github.com/<所有者>/<仓库> 地址重新登记', 'detail': ''}
        if github:
            token = self._github_token()
            if token:
                from factory.control.github import github_git_env
                header = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
                return github_git_env(token), 'github', (token, header)
            if credential_ref == 'github':
                return {'reason': 'credential_missing', 'message': '服务端尚未配置 FACTORY_GITHUB_TOKEN',
                        'next_step': '管理员在服务端配置 FACTORY_GITHUB_TOKEN 后点“重新接入”', 'detail': ''}
        # Other hosts and SSH: the execution host's own git setup authorises the clone.
        return {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, None, ()

    def _fail_clone(self, project_id, failure, credential=None):
        record = self._probe_record(project_id) or {}
        record.update({'state': 'failed', 'at': now(),
                       'access': {'ok': False, 'credential': credential, **failure}})
        self._save_probe(project_id, record)

    def _clone_and_probe(self, project_id, url, target, branch):
        record = self._probe_record(project_id) or {}
        resolved = self._clone_env(url, record.get('credential_ref'))
        if isinstance(resolved, dict):
            return self._fail_clone(project_id, resolved)
        env, credential, hidden = resolved
        target = Path(target)
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            # Never overwrite: whatever is there is someone's checkout or data.
            return self._fail_clone(project_id, {
                'reason': 'target_occupied', 'message': '执行主机上的目标目录已存在且非空，未覆盖',
                'next_step': '管理员核对并清理该目录后点“重新接入”，或改用本地目录方式登记',
                'detail': str(target)}, credential)
        args = ['clone', '--no-tags', '--single-branch']
        if branch:
            args += ['--branch', branch]
        target.parent.mkdir(parents=True, exist_ok=True)
        # Clone beside the target and move it in only when complete, so a failed
        # or interrupted clone never leaves a half-filled workspace behind.
        staging = target.parent / f'.clone-{target.name}-{uuid.uuid4().hex[:8]}'
        failure = None
        try:
            done = subprocess.run(['git', *args, url, str(staging)], capture_output=True, text=True,
                                  timeout=int(os.getenv('FACTORY_CLONE_TIMEOUT', '600')), env=env)
            if done.returncode != 0:
                failure = _clone_failure(done.stderr, credential=credential,
                                         github=bool(_GITHUB_HTTPS.match(url)), hidden=hidden)
        except subprocess.TimeoutExpired:
            failure = _clone_failure('', timed_out=True)
        except OSError as exc:
            failure = _clone_failure(f'{type(exc).__name__}', hidden=hidden)
        if failure is None:
            try:
                if target.is_dir():
                    target.rmdir()  # empty (checked above); rmdir refuses anything else
                staging.rename(target)
            except OSError as exc:
                failure = {'reason': 'target_occupied', 'message': '克隆完成但无法放入目标目录，未覆盖',
                           'next_step': '管理员核对该目录后点“重新接入”', 'detail': type(exc).__name__}
        if failure is not None:
            shutil.rmtree(staging, ignore_errors=True)
            return self._fail_clone(project_id, failure, credential)
        record = self._probe_record(project_id) or {}
        record['credential_used'] = credential
        self._save_probe(project_id, record)
        # The baseline is what was actually cloned: the requested branch, or the
        # remote default when none was requested.
        head = _git(['symbolic-ref', '--short', 'HEAD'], target)
        cloned = head.stdout.strip() if _ok(head) and head.stdout.strip() else branch
        if cloned:
            project = self.store.project(project_id)
            if project['base_branch'] != cloned:
                self.store.update_project(project_id, {'base_branch': cloned},
                                          project['revision'], 'maintenance-subsystem')
        self.probe(project_id, actor=None)

    def probe(self, project_id, *, actor, background=True) -> dict:
        """Inspect the checkout the executor will use. Nothing here runs project code.

        A URL-registered repository whose clone failed is cloned again (same
        project id, same workspace) when a person asks for a new probe.
        """
        if actor is not None:
            self._require(actor, project_id)
        project = self.store.project(project_id)
        record = self._probe_record(project_id) or {}
        with self._analysis_lock:
            cloning = project_id in self._cloning
        if cloning and actor is not None:
            # A clone in flight owns the record; a person's probe reports it
            # instead of racing it. The clone's own final probe (actor=None) runs.
            return self.repo_view(project_id, actor=actor)
        if actor is not None and self._needs_clone(project, record):
            self._start_clone(project_id, background=background)
            return self.repo_view(project_id, actor=actor)
        root = Path(project['workspace']).expanduser()
        findings = []

        def finding(fid, label, status, message):
            findings.append({'id': fid, 'label': label, 'status': status, 'message': message})

        access_ok = root.is_dir() and _ok(_git(['rev-parse', '--git-dir'], root))
        head_sha = branch_sha = remote = None
        dirty = False
        stack, suggested = [], []
        if not access_ok:
            finding('access', '仓库访问', 'failed', '执行主机上的工作区不存在或不是 Git 仓库')
        else:
            finding('access', '仓库访问', 'verified', '执行主机可读取该 Git 仓库')
            ref = _git(['rev-parse', '--verify', f"refs/heads/{project['base_branch']}"], root)
            if _ok(ref):
                branch_sha = ref.stdout.strip()
                finding('baseline', '基线版本', 'verified',
                        f"分支 {project['base_branch']} 当前为 {branch_sha[:12]}，新需求以此为基线")
            else:
                finding('baseline', '基线版本', 'failed', f"本地分支 {project['base_branch']} 不存在")
            head = _git(['rev-parse', 'HEAD'], root)
            head_sha = head.stdout.strip() if _ok(head) else None
            origin = _git(['remote', 'get-url', 'origin'], root)
            remote = origin.stdout.strip() if _ok(origin) and origin.stdout.strip() else None
            # Only the host part is shown: a URL can carry an embedded credential.
            if remote:
                remote = re.sub(r'//[^/@]+@', '//', remote)
            status = _git(['status', '--porcelain=v1'], root)
            dirty = _ok(status) and bool(status.stdout.strip())
            if dirty:
                # The planner refuses a dirty checkout, so this is a blocker, not a note.
                finding('clean', '工作区', 'failed', '工作区有未提交或未跟踪的改动；规划前必须清理或提交（可加入 .gitignore）')
            elif _ok(status):
                finding('clean', '工作区', 'verified', '工作区干净')
            stack, suggested = detect_stack(root)
            for item in stack:
                finding(f'stack:{item["name"]}', f'技术栈 {item["name"]}', 'found',
                        f'依据 {item["evidence"]}；已发现，不等于环境可运行')
            for check in suggested:
                available, note = _check_runnable(check['argv'], root)
                check['available'] = available
                display_argv = ('Docker 容器内：' + ' '.join(check['argv'][1:])
                                if check['argv'][0] == '@dockerfile' else ' '.join(check['argv']))
                finding(f'suggest:{check["name"]}', f'建议检查 {check["name"]}',
                        'found' if available else 'failed',
                        f"{display_argv}（{check['evidence']}）；{note}")
        configured = sorted((project.get('checks') or {}).keys())
        host_pytest = ((project.get('checks') or {}).get('pytest') in
                       (['python3', '-m', 'pytest', '-q'], ['uv', 'run', 'pytest', '-q']))
        docker_pytest = any(c['name'] == 'pytest' and c['argv'][0] == '@dockerfile' for c in suggested)
        if configured:
            finding('checks', '项目检查', 'found', f"已配置 {len(configured)} 条：{'、'.join(configured)}（尚未在本次探测中运行）")
        else:
            finding('checks', '项目检查', 'missing', '还没有可信检查命令；执行结果将无法被项目检查验证')
        needs = []
        if access_ok and dirty:
            needs.append('清理工作区的未提交/未跟踪改动')
        if access_ok and not branch_sha:
            needs.append('指定存在的基线分支')
        if not configured:
            needs.append('确认检查命令' + ('（可采纳系统建议）' if suggested else ''))
        if docker_pytest and host_pytest:
            finding('check-backend', '检查执行环境', 'failed',
                    '已配置的 pytest 仍在宿主机运行；请采纳 Docker 容器建议检查以切换，不能仅凭 Dockerfile 认定已切换')
            needs.append('将 pytest 检查切换到 Docker 容器')
        state = 'failed' if not access_ok else ('needs_input' if needs else 'ready')
        record.update({
            'state': state, 'at': now(), 'head_sha': head_sha, 'branch': project['base_branch'],
            'base_sha': branch_sha, 'remote': remote,
            'access': {'ok': True, 'message': '可访问'} if access_ok else (
                record.get('access') if (record.get('access') or {}).get('reason') else
                {'ok': False, 'reason': 'workspace_missing', 'message': '执行主机上的工作区不存在或不是 Git 仓库',
                 'next_step': '点“重新接入”重新拉取，或核对本地目录'}),
            'stack': stack, 'suggested_checks': suggested, 'findings': findings, 'needs': needs})
        self._save_probe(project_id, record)
        return self.repo_view(project_id, actor=actor) if actor is not None else record

    def adopt_checks(self, project_id, names, *, actor) -> dict:
        self._require(actor, project_id)
        record = self._probe_record(project_id) or {}
        suggested = {c['name']: c['argv'] for c in record.get('suggested_checks') or []}
        chosen = {n: suggested[n] for n in names if n in suggested}
        if not chosen:
            raise ValueError('没有可采纳的建议检查；请先重新分析')
        project = self.store.project(project_id)
        checks = {**(project.get('checks') or {}), **chosen}
        self.store.update_project(project_id, {'checks': checks}, project['revision'],
                                  actor.get('username', 'system'))
        return self.probe(project_id, actor=actor)

    def _memory(self, project_id) -> dict:
        try:
            from factory.control import scenario_memory
            entries = scenario_memory.recall(self.store, project_id, plugin_id=PLUGIN_ID)
            confirmed = sum(1 for e in entries if scenario_memory.role_of(e) == 'constraint')
            return {'entries': len(entries), 'confirmed': confirmed, 'code_index': 'on_demand'}
        except Exception:  # noqa: BLE001 - memory unreadable is reported, not fatal
            return {'entries': None, 'confirmed': None, 'code_index': 'on_demand'}

    def repo_view(self, project_id, *, actor, detail=False) -> dict:
        if actor is not None:
            self._require(actor, project_id)
        project = self.store.project(project_id)
        record = self._probe_record(project_id) or {'state': 'pending'}
        state = record.get('state', 'pending')
        probe = None
        if record.get('at') or record.get('access'):
            probe = {'at': record.get('at'), 'head_sha': record.get('head_sha'),
                     'branch': record.get('branch'), 'remote': record.get('remote'),
                     'access': record.get('access') or {'ok': False, 'message': '尚未探测'},
                     'stack': record.get('stack') or [],
                     'suggested_checks': record.get('suggested_checks') or [],
                     'findings': record.get('findings') or []}
        view = {'project_id': project_id, 'name': project['name'],
                'repository': project['repository'], 'workspace': project['workspace'],
                'base_branch': project['base_branch'], 'state': state,
                'state_label': REPO_STATE_LABEL.get(state, state), 'probe': probe,
                'needs': record.get('needs') or [],
                'checks_configured': sorted((project.get('checks') or {}).keys()),
                'memory': self._memory(project_id),
                'credential_ref': record.get('credential_ref'),
                'synthetic': bool(record.get('synthetic'))}
        if detail:
            view['requirements'] = self.requirements(actor=actor, project_id=project_id)
            view['tasks'] = [{'task_id': t['record']['id'], 'title': t['record']['issue']['title'],
                              'status': t['status'], 'created_at': t['record']['created_at']}
                             for t in self._task_rows([project_id])]
        return view

    def repos(self, *, actor) -> list[dict]:
        return [self.repo_view(pid, actor=None) for pid in self._scope(actor)]

    # -- intake sources ------------------------------------------------------
    def create_source(self, *, name, project_ids, auto_dispatch, actor, synthetic=False) -> dict:
        name = str(name or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_.\-一-鿿]{1,60}', name):
            raise ValueError('来源名称需为 1-60 个字母、数字、中文、点、下划线或连字符')
        if not project_ids:
            raise ValueError('至少选择一个允许提交的项目')
        for pid in project_ids:
            self._require(actor, pid)
        token = 'wbm_' + secrets.token_urlsafe(32)
        source = {'id': uuid.uuid4().hex, 'name': name, 'project_ids': sorted(set(project_ids)),
                  'auto_dispatch': bool(auto_dispatch), 'synthetic': bool(synthetic),
                  'created_at': now(), 'revoked_at': None,
                  'created_by': actor.get('username'), 'created_by_id': actor.get('id'),
                  'token_hint': token[-4:]}
        with self.store.connect() as db:
            for row in db.execute('SELECT data FROM maintenance_intake_sources'):
                other = json.loads(row[0])
                if other['name'] == name and not other.get('revoked_at'):
                    raise Conflict('同名的接入来源已存在；请先吊销或换一个名称')
            db.execute('INSERT INTO maintenance_intake_sources VALUES (?,?,?)',
                       (source['id'], _sha256(token), json.dumps(source, ensure_ascii=False)))
        return {**self._source_view(source), 'token': token}

    @staticmethod
    def _source_view(source):
        return {**{k: source[k] for k in ('id', 'name', 'project_ids', 'auto_dispatch',
                                          'created_at', 'revoked_at', 'token_hint')},
                'synthetic': bool(source.get('synthetic'))}

    def sources(self) -> list[dict]:
        with self.store.connect() as db:
            return [self._source_view(json.loads(r[0]))
                    for r in db.execute('SELECT data FROM maintenance_intake_sources ORDER BY rowid DESC')]

    def revoke_source(self, source_id) -> dict:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM maintenance_intake_sources WHERE id=?',
                             (source_id,)).fetchone()
            if row is None:
                raise KeyError(source_id)
            source = json.loads(row[0])
            source['revoked_at'] = source.get('revoked_at') or now()
            db.execute('UPDATE maintenance_intake_sources SET data=? WHERE id=?',
                       (json.dumps(source, ensure_ascii=False), source_id))
        return self._source_view(source)

    def authenticate_source(self, token: str) -> dict | None:
        if not token or not token.startswith('wbm_'):
            return None
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM maintenance_intake_sources WHERE token_hash=?',
                             (_sha256(token),)).fetchone()
        if row is None:
            return None
        source = json.loads(row[0])
        return None if source.get('revoked_at') else source

    # -- requirements --------------------------------------------------------
    def submit(self, *, project_id, content, source_kind, source_name, actor,
               external_id=None, idempotency_key=None, title=None, attachments=(),
               auto_dispatch=True, synthetic=False) -> tuple[dict, bool]:
        """The one intake: a human, a CLI and another system all come through here.

        Returns ``(receipt, created)``. Received is not executed: the receipt says
        which of the two happened.
        """
        if source_kind not in SOURCE_KINDS:
            raise ValueError('未知的需求来源类型')
        content = str(content or '').strip()
        if not content:
            raise ValueError('需求内容不能为空')
        if len(content) > 20_000:
            raise ValueError('需求内容过长（上限 20000 字）')
        if external_id is not None and not re.fullmatch(r'[A-Za-z0-9_.:#/-]{1,120}', str(external_id)):
            raise ValueError('外部事件标识需为 1-120 位字母、数字或 _.:#/-')
        if idempotency_key is not None and not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', str(idempotency_key)):
            raise ValueError('幂等键需为 8-100 位字母、数字、下划线或连字符')
        clean_attachments = []
        for item in attachments or ():
            if not isinstance(item, dict) or not item.get('ref'):
                raise ValueError('附件只接受受控引用 {name, ref}')
            ref = str(item['ref'])
            if len(ref) > 500 or re.search(r'(?i)(token|secret|password|key)=', ref):
                raise ValueError('附件引用不能携带凭据')
            clean_attachments.append({'name': str(item.get('name') or ref)[:200], 'ref': ref})
        self._require(actor, project_id)
        # A stopped plugin refuses new work at the door, not after a record exists.
        self.tasks.gate.require('create')
        title = (str(title).strip() if title else content.splitlines()[0])[:120]
        scope = f'{source_kind}:{source_name}|{project_id}'
        idem = (f'{scope}|ext:{external_id}' if external_id else
                f'{scope}|key:{idempotency_key}' if idempotency_key else None)
        digest = _sha256(json.dumps([title, content, clean_attachments], ensure_ascii=False))
        record = {'id': uuid.uuid4().hex, 'project_id': project_id,
                  'source': {'kind': source_kind, 'name': source_name},
                  'external_id': external_id, 'title': title, 'content': content,
                  'content_digest': digest, 'attachments': clean_attachments,
                  'received_at': now(), 'status': 'received', 'task_id': None,
                  'dispatch_error': None, 'submitted_by': actor.get('username'),
                  # Declared by the source or by the repository's registration.
                  'synthetic': bool(synthetic) or self._repo_synthetic(project_id),
                  'submitted_by_id': actor.get('id')}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if idem:
                row = db.execute('SELECT data FROM maintenance_requirements WHERE idem_key=?',
                                 (idem,)).fetchone()
                if row is not None:
                    existing = json.loads(row[0])
                    if existing['content_digest'] != digest:
                        raise Conflict('同一来源、项目和事件标识已提交过不同内容；不会覆盖原记录，'
                                       '请换一个事件标识作为新需求提交')
                    db.execute('ROLLBACK')
                    return self.receipt(existing, duplicate=True), False
            db.execute('INSERT INTO maintenance_requirements VALUES (?,?,?,?,?)',
                       (record['id'], project_id, idem, record['received_at'],
                        json.dumps(record, ensure_ascii=False)))
        if auto_dispatch:
            record = self.dispatch(record['id'], actor=actor)
        else:
            record = self._update_requirement(record['id'], {'status': 'pending_dispatch'})
        return self.receipt(record), True

    def _requirement(self, requirement_id) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM maintenance_requirements WHERE id=?',
                             (requirement_id,)).fetchone()
        if row is None:
            raise KeyError(requirement_id)
        return json.loads(row[0])

    def _update_requirement(self, requirement_id, changes) -> dict:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM maintenance_requirements WHERE id=?',
                             (requirement_id,)).fetchone()
            if row is None:
                raise KeyError(requirement_id)
            record = {**json.loads(row[0]), **changes}
            db.execute('UPDATE maintenance_requirements SET data=? WHERE id=?',
                       (json.dumps(record, ensure_ascii=False), requirement_id))
        return record

    def agreement(self) -> dict:
        from factory.control import agent_packs
        from factory.control.issue_maintenance import SCHEMA_VERSION
        pack = next((p for p in agent_packs.catalog() if p['id'] == PLUGIN_ID), None)
        if pack is None:
            raise Conflict('没有安装 Issue 维护方法包，暂时不能派发维护任务')
        return {'revision': f'v{SCHEMA_VERSION}', 'skill_version': f"{pack['id']}@{pack['version']}"}

    def _baseline(self, project) -> str:
        root = Path(project['workspace']).expanduser()
        done = _git(['rev-parse', '--verify', f"refs/heads/{project['base_branch']}"], root)
        if not _ok(done):
            raise Conflict(f"无法解析项目基线分支 {project['base_branch']}；请在维护代码库里重新分析")
        return done.stdout.strip()

    def _task_request(self, record, project, *, key, body=None, version='1') -> dict:
        lines = [body if body is not None else record['content']]
        if record.get('attachments'):
            lines += ['', '附件（受控引用）：'] + [f"- {a['name']}: {a['ref']}" for a in record['attachments']]
        source = record['source']
        return {
            'issue': {'source': f"{source['kind']}:{source['name']}",
                      'external_id': record.get('external_id') or record['id'],
                      'version': version, 'title': record['title'], 'body': '\n'.join(lines)},
            'project_id': project['id'], 'repository': project['repository'],
            'base_sha': self._baseline(project),
            'base_branch_label': project['base_branch'],
            'expected_behaviour': '以需求原文为准；信息不足时先向业务提问，再动手修改',
            # Worded to stay clear of the planner's high-risk vocabulary: a fixed
            # phrase here would force every intake task to human approval for a
            # reason the requirement itself never gave.
            'delivery_goal': '可审阅的补丁包与项目检查结果（只交付补丁，不上线）',
            'agreement': self.agreement(), 'idempotency_key': key,
            'delivery_tier': 'package', 'synthetic': bool(record.get('synthetic'))}

    def dispatch(self, requirement_id, *, actor) -> dict:
        """Turn a received requirement into the existing maintenance task, once."""
        record = self._requirement(requirement_id)
        self._require(actor, record['project_id'])
        if record.get('task_id'):
            return record
        try:
            project = self.store.project(record['project_id'])
            view = self.tasks.create(self._task_request(record, project, key=f"req-{record['id']}"),
                                     actor=actor)
        except (Conflict, ValueError, KeyError) as exc:
            return self._update_requirement(requirement_id, {
                'status': 'dispatch_failed', 'dispatch_error': str(exc)[:500]})
        return self._update_requirement(requirement_id, {
            'status': 'dispatched', 'task_id': view['task_id'], 'dispatch_error': None,
            'dispatched_at': now(), 'dispatched_by': actor.get('username')})

    def _head(self, task_id):
        from factory.control.issue_maintenance import MaintenanceStore
        records = MaintenanceStore(self.store)
        record = records.get(task_id)
        seen = set()
        while record.get('successor_id') and record['id'] not in seen:
            seen.add(record['id'])
            record = records.get(record['successor_id'])
        return record

    def _clarification(self, task_id):
        if not task_id:
            return {'state': 'analysis_pending', 'questions': []}
        record = self._head(task_id)
        run = self._run(record.get('execution_id'))
        if run and run.get('status') == 'needs_clarification':
            return {'state': 'questions', 'questions': list((run.get('plan') or {}).get('questions') or [])}
        if run and run.get('status') in _ACTIVE | {'received'} and not run.get('plan'):
            return {'state': 'analysis_pending', 'questions': []}
        return {'state': 'not_needed', 'questions': []}

    def _run(self, execution_id):
        if not execution_id:
            return None
        try:
            return self.store.get(execution_id)
        except KeyError:
            return None

    def receipt(self, record, *, duplicate=False) -> dict:
        task_id = record.get('task_id')
        execution_id = None
        if task_id:
            try:
                execution_id = self._head(task_id).get('execution_id')
            except KeyError:
                execution_id = None
        message = {'dispatched': '已接收并派发执行；接收不等于完成，进度见任务现场',
                   'pending_dispatch': '已接收；该来源未开启自动执行，需有权限的人派发',
                   'dispatch_failed': '已接收但派发失败：' + (record.get('dispatch_error') or ''),
                   'received': '已接收'}.get(record['status'], '已接收')
        return {'requirement_id': record['id'], 'status': record['status'],
                'duplicate': duplicate, 'task_id': task_id, 'execution_id': execution_id,
                'clarification': self._clarification(task_id),
                'received_at': record['received_at'], 'source': record['source'],
                'project_id': record['project_id'], 'message': message,
                'synthetic': bool(record.get('synthetic'))}

    def requirement_view(self, record, names=None) -> dict:
        task_status = None
        if record.get('task_id'):
            try:
                task_status = self._task_status(self._head(record['task_id']))
            except KeyError:
                task_status = None
        return {'requirement_id': record['id'], 'project_id': record['project_id'],
                'project_name': (names or {}).get(record['project_id']),
                'source': record['source'], 'external_id': record.get('external_id'),
                'title': record['title'], 'content': record['content'],
                'attachments': record.get('attachments') or [],
                'received_at': record['received_at'], 'status': record['status'],
                'status_label': REQUIREMENT_STATUS_LABEL.get(record['status'], record['status']),
                'task_id': record.get('task_id'), 'task_status': task_status,
                'dispatch_error': record.get('dispatch_error'),
                'synthetic': bool(record.get('synthetic'))}

    def requirements(self, *, actor, project_id=None) -> list[dict]:
        scope = set(self._scope(actor, project_id)) if actor is not None else None
        names = {p['id']: p['name'] for p in self.store.projects()}
        with self.store.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute(
                'SELECT data FROM maintenance_requirements ORDER BY received_at DESC LIMIT 500')]
        return [self.requirement_view(r, names) for r in rows
                if (scope is None or r['project_id'] in scope)
                and (project_id is None or r['project_id'] == project_id)]

    # -- tasks ---------------------------------------------------------------
    def _task_status(self, record) -> str:
        run = self._run(record.get('execution_id'))
        return run['status'] if run else 'received'

    def _task_rows(self, project_ids, *, include_superseded=False):
        """Head revisions by default: a revised task is one piece of work, not two.

        Deliveries are the exception -- a revision that delivered and was then
        followed up still delivered -- so callers counting artifacts ask for all.
        """
        from factory.control.issue_maintenance import MaintenanceStore
        wanted = set(project_ids)
        rows = []
        for record in MaintenanceStore(self.store).tasks():
            if record['project_id'] not in wanted or (record.get('successor_id') and not include_superseded):
                continue
            run = self._run(record.get('execution_id'))
            status = run['status'] if run else ('received' if not record.get('execution_id') else 'missing')
            rows.append({'record': record, 'run': run, 'status': status})
        return rows

    def task_actions(self, view) -> list[str]:
        """What a caller may do now, from the execution's real state. No ``pause``."""
        run = self._run(view.get('execution_id'))
        status = run['status'] if run else None
        actions = []
        if status in _ANSWER:
            actions.append('answer')
        if status in _APPROVAL and status == 'awaiting_approval':
            actions.append('approve')
        if status == 'needs_human':
            actions.append('supplement')
            if run.get('plan'):
                actions.append('resume')
        if view.get('status') in ('received', 'running', 'waiting'):
            actions.append('cancel')
        if view.get('status') == 'delivered':
            actions.append('export')
        if not view.get('successor_id') and (view.get('status') in ('delivered', 'failed', 'cancelled')
                                             or self._stopped_before_plan(run)):
            actions.append('feedback')
        return actions

    @staticmethod
    def _stopped_before_plan(run) -> bool:
        """Stopped at a human gate with no plan: ``resume`` cannot continue it."""
        return bool(run) and run.get('status') == 'needs_human' and not run.get('plan')

    def feedback(self, task_id, content, *, actor) -> dict:
        """Follow-up on a finished task: a new revision linked to the old one.

        The original process is not resumed in place -- the executor cannot do
        that -- so the successor says so in its own text and in ``predecessor_id``.
        """
        from factory.control.issue_maintenance import MaintenanceStore
        content = str(content or '').strip()
        if not content:
            raise ValueError('反馈内容不能为空')
        previous = MaintenanceStore(self.store).get(task_id)
        self._require(actor, previous['project_id'])
        if previous.get('successor_id'):
            raise Conflict('这个任务已经有后续修订，请在最新修订上反馈')
        view = self.tasks.get(task_id, actor=actor)
        restart = self._stopped_before_plan(self._run(view.get('execution_id')))
        if view['status'] not in ('delivered', 'failed', 'cancelled') and not restart:
            raise Conflict('任务尚未结束；进行中的任务请使用回答或补充信息')
        if restart:
            # The old execution never got a plan and cannot be resumed in place, so
            # it is stopped for good before the linked revision starts over.
            self.tasks.cancel(task_id, actor=actor)
        project = self.store.project(previous['project_id'])
        delivered = (view.get('delivery') or {}).get('commit') if view['status'] == 'delivered' else None
        continue_from = None
        if delivered:
            # Keep what was delivered: the revision works on the delivered working
            # copy and its patch applies on top of that commit. If that copy is
            # gone this refuses -- redoing from the old baseline would silently
            # drop the delivered change.
            continue_from = {'execution_id': view['execution_id'], 'commit': delivered,
                             'chain_base_sha': (previous.get('continue_from') or {}).get('chain_base_sha')
                             or previous['base_sha']}
            self.tasks.port.execution._require_continuable(continue_from)
        body = previous['issue']['body'] + '\n\n--- 后续反馈（第 %d 次修订）---\n' % (previous['revision'] + 1)
        if delivered:
            body += (f'本修订在上一修订交付的 commit {delivered} 之上继续，已包含其全部改动；'
                     '只追加本次反馈要求的修改，不要撤销已交付的内容。\n')
        body += content
        record = {'id': previous['id'], 'title': previous['issue']['title'],
                  'content': body, 'attachments': [],
                  'external_id': previous['issue']['external_id'],
                  'source': {'kind': previous['issue']['source'].split(':', 1)[0]
                             if ':' in previous['issue']['source'] else 'manual',
                             'name': previous['issue']['source'].split(':', 1)[-1]}}
        request = self._task_request(record, project, body=body,
                                     key='fb-' + _sha256(task_id + content)[:40],
                                     version=str(int(previous['issue']['version'] or 1) + 1)
                                     if str(previous['issue']['version']).isdigit() else '2')
        request['issue']['source'] = previous['issue']['source']
        request['synthetic'] = bool(previous.get('synthetic'))
        if continue_from:
            request['base_sha'] = delivered
            request['continue_from'] = continue_from
        return self.tasks.revise(task_id, request, actor=actor)

    # -- monitor -------------------------------------------------------------
    def executor_status(self) -> dict:
        return executor_status(self.svc, self.store)

    def _queue_counts(self):
        with self.store.connect() as db:
            rows = dict(db.execute("SELECT status,COUNT(*) FROM control_jobs "
                                   "WHERE status IN ('pending','running') GROUP BY status").fetchall())
        return rows.get('pending', 0), rows.get('running', 0)

    def _delivered_at(self, run):
        for event in reversed(self.store.events(run['id'], limit=2000)):
            if event['type'] == 'run.verified':
                return event['at']
        return None

    @staticmethod
    def _attention_kind(status):
        if status in _ANSWER:
            return 'answer'
        if status in _APPROVAL:
            return 'approval'
        if status in _BLOCKED or status == 'missing':
            return 'blocked'
        return None

    def _reason(self, row, kind):
        run = row['run']
        if kind == 'answer':
            questions = (run.get('plan') or {}).get('questions') or []
            first = questions[0] if questions else None
            text = first.get('question') if isinstance(first, dict) else first
            return str(text or '模型在动手前提出了需要业务确认的问题')[:200]
        if kind == 'approval':
            return '计划已就绪，等待批准后开始修改'
        if row['status'] == 'missing':
            return '执行记录不可读，需人工核对'
        return str((run or {}).get('error') or ('执行失败' if row['status'] == 'failed' else '执行已停下，等待人工处理'))[:200]

    def overview(self, *, actor, project_id=None) -> dict:
        errors = []
        scope = self._scope(actor, project_id)
        projects = {p['id']: p for p in self.store.projects() if p['id'] in scope}
        start, end = _day_window()
        rows = self._task_rows(scope)
        running = sum(1 for r in rows if r['status'] in _ACTIVE)
        attention, counts = [], {'answer': 0, 'approval': 0, 'blocked': 0}
        delivered_window = 0
        per_project = {pid: {'running': 0, 'waiting': 0, 'delivered': 0} for pid in scope}
        for row in rows:
            record, status = row['record'], row['status']
            stats = per_project[record['project_id']]
            kind = self._attention_kind(status)
            if status in _ACTIVE:
                stats['running'] += 1
            if kind:
                stats['waiting'] += 1
                counts[kind] += 1
                attention.append({'task_id': record['id'], 'project_id': record['project_id'],
                                  'project_name': projects[record['project_id']]['name'],
                                  'title': record['issue']['title'], 'kind': kind,
                                  'reason': self._reason(row, kind),
                                  'since': (row['run'] or {}).get('updated_at') or record['created_at']})
        # Every revision whose execution produced its artifact counts once, even if
        # a follow-up revision has since superseded it as the live head.
        for row in self._task_rows(scope, include_superseded=True):
            if row['status'] in _DELIVERED:
                per_project[row['record']['project_id']]['delivered'] += 1
                at = _parse(self._delivered_at(row['run']))
                if at and start <= at.astimezone(start.tzinfo) < end:
                    delivered_window += 1
        attention.sort(key=lambda a: a['since'] or '', reverse=True)
        try:
            events = self._events(rows, projects)
        except Exception as exc:  # noqa: BLE001 - a partial failure must not blank the page
            events = []
            errors.append({'section': 'events', 'message': f'事件读取失败：{type(exc).__name__}'})
        executor = self.executor_status()
        try:
            pending, active_jobs = self._queue_counts()
        except Exception as exc:  # noqa: BLE001
            pending = active_jobs = None
            errors.append({'section': 'queue', 'message': f'队列读取失败：{type(exc).__name__}'})
        try:
            availability = self.tasks.gate.availability.view(self.tasks.gate.plugin_id)
        except Exception:  # noqa: BLE001
            availability = {'state': 'unknown'}
        repo_rows = []
        for pid in scope:
            record = self._probe_record(pid) or {'state': 'pending'}
            state = record.get('state', 'pending')
            repo_rows.append({'project_id': pid, 'name': projects[pid]['name'],
                              'repository': projects[pid]['repository'],
                              'repo_state': state, 'repo_state_label': REPO_STATE_LABEL.get(state, state),
                              **per_project[pid], 'delivery_target': '补丁包',
                              'service_status': 'not_connected',
                              'synthetic': bool(record.get('synthetic'))})
        return {
            'contract_version': CONTRACT_VERSION, 'generated_at': now(),
            'window': {'kind': 'day', 'start': start.isoformat(), 'end': end.isoformat(),
                       'timezone': local_timezone()},
            'scope': {'project_ids': scope},
            'sources': {'maintenance': {'status': 'connected', 'detail': '维护任务与执行记录'},
                        'executor': executor,
                        'server_metrics': {'status': 'not_connected', 'detail': '未配置 CPU/内存采集源'},
                        'app_probes': {'status': 'not_connected', 'detail': '未配置应用探针'},
                        'alerts': {'status': 'not_connected', 'detail': '未配置告警源'}},
            'counts': {'projects': len(scope), 'running': running,
                       'attention': {'total': sum(counts.values()), **counts},
                       'delivered_in_window': delivered_window},
            'attention': attention[:50], 'projects': repo_rows, 'events': events,
            'queue': {'pending': pending, 'running': active_jobs, 'executor': executor},
            'availability': availability, 'partial_errors': errors}

    def _events(self, rows, projects, limit=20):
        ids = {r['record']['execution_id']: r['record'] for r in rows if r['record'].get('execution_id')}
        if not ids:
            return []
        marks = ','.join('?' * len(ids))
        # Business milestones only: provider timing, raw SDK frames and usage rows
        # stay in the task's own log, where they can be read in context.
        kinds = sorted(_FEED_KINDS)
        kind_marks = ','.join('?' * len(kinds))
        with self.store.connect() as db:
            found = db.execute(f'SELECT id,run_id,type,at FROM events WHERE run_id IN ({marks}) '
                               f'AND type IN ({kind_marks}) ORDER BY id DESC LIMIT ?',
                               (*ids, *kinds, limit)).fetchall()
        return [{'task_id': ids[e['run_id']]['id'], 'project_id': ids[e['run_id']]['project_id'],
                 'execution_id': e['run_id'], 'sequence': e['id'], 'kind': e['type'],
                 'label': f"{ids[e['run_id']]['issue']['title'][:40]} · {_EVENT_LABELS.get(e['type'], e['type'])}",
                 'at': e['at']} for e in found]

    def graph(self, *, actor, project_id=None, max_tasks=200) -> dict:
        """Read-only relation projection: repo → requirement → task → plan → check → patch.

        Every fact comes from a stored record; a stage without a record says so
        rather than being inferred. Scope is the same ``_scope`` every read uses,
        so nothing from a project the caller may not read is counted or named.
        """
        from factory.control.engineering_overview import current_evidence
        from factory.control.issue_maintenance import MaintenanceStore
        scope = self._scope(actor, project_id)
        projects = {p['id']: p for p in self.store.projects() if p['id'] in scope}
        nodes, edges = [], []
        unrecorded = '未记录（系统没有该字段）'

        def add(node, parent=None, kind=None):
            nodes.append(node)
            if parent:
                edges.append({'from': parent, 'to': node['id'], 'kind': kind})

        for pid in scope:
            record = self._probe_record(pid) or {'state': 'pending'}
            state = record.get('state', 'pending')
            project = projects[pid]
            base = record.get('base_sha') or record.get('head_sha')
            add({'id': f'repo:{pid}', 'type': 'repo', 'label': project['name'],
                 'sublabel': ('合成 · ' if record.get('synthetic') else '')
                 + f"{project['repository']} · {REPO_STATE_LABEL.get(state, '待分析')}",
                 'status': state, 'task_id': None, 'project_id': pid, 'synthetic': bool(record.get('synthetic')),
                 'tone': {'ready': 'ok', 'needs_input': 'attention', 'failed': 'blocked'}.get(state, 'pending'),
                 'facts': [{'label': '仓库', 'value': project['repository']},
                           {'label': '基线分支', 'value': project.get('base_branch') or '未记录'},
                           {'label': '基线版本', 'value': base[:12] if base else '尚未分析'},
                           {'label': '接入状态', 'value': REPO_STATE_LABEL.get(state, state)},
                           {'label': '待补充', 'value': '、'.join(record.get('needs') or []) or '无'},
                           *([{'label': '失败原因', 'value': access.get('message') or '未记录'},
                              {'label': '下一步', 'value': access.get('next_step') or '查看仓库详情'}]
                             if state == 'failed' and (access := record.get('access') or {}) and not access.get('ok') else []),
                           {'label': '负责人', 'value': unrecorded},
                           {'label': '登记人', 'value': project.get('actor') or '未记录'}],
                 'links': [{'kind': 'repo', 'id': pid, 'label': '仓库详情'}]})
        rows = sorted(self._task_rows(scope), key=lambda r: r['record']['created_at'], reverse=True)
        truncated = len(rows) > max_tasks
        rows = rows[:max_tasks]
        tasks = MaintenanceStore(self.store)
        chain_root = {}
        for row in rows:
            record, seen = row['record'], set()
            while record.get('predecessor_id') and record['id'] not in seen:
                seen.add(record['id'])
                record = tasks.get(record['predecessor_id'])
            chain_root[record['id']] = row
        requirement_of = {}
        with self.store.connect() as db:
            requirements = [json.loads(r[0]) for r in db.execute('SELECT data FROM maintenance_requirements')]
        for req in requirements:
            if req['project_id'] not in projects:
                continue
            source = req.get('source') or {}
            kind_label = {'manual': '人工提交', 'api': '外部系统', 'cli': 'CLI'}.get(source.get('kind'), source.get('kind'))
            add({'id': f"req:{req['id']}", 'type': 'requirement', 'label': req['title'][:60], 'full_label': req['title'],
                 'sublabel': f"{kind_label} · {REQUIREMENT_STATUS_LABEL.get(req['status'], req['status'])}",
                 'status': req['status'], 'task_id': req.get('task_id'), 'project_id': req['project_id'],
                 'synthetic': bool(req.get('synthetic')),
                 'tone': {'dispatched': 'ok', 'dispatch_failed': 'blocked'}.get(req['status'], 'pending'),
                 'facts': [{'label': '来源', 'value': f"{kind_label}（{source.get('name') or '未记录'}）"},
                           {'label': '接收时间', 'value': req.get('received_at') or req.get('created_at') or '未记录'},
                           {'label': '状态', 'value': REQUIREMENT_STATUS_LABEL.get(req['status'], req['status'])},
                           {'label': '需求 ID', 'value': req['id']}],
                 'links': [{'kind': 'task', 'id': req['task_id'], 'label': '任务现场'}] if req.get('task_id') else []},
                f"repo:{req['project_id']}", 'has')
            if req.get('task_id'):
                requirement_of[req['task_id']] = req['id']
        head_ids = {row['record']['id'] for row in chain_root.values()}
        history = {}
        for row in self._task_rows(scope, include_superseded=True):
            record = row['record']
            commit = ((row['run'] or {}).get('artifacts') or {}).get('commit')
            if record.get('successor_id') and row['status'] in _DELIVERED and commit:
                head = self._head(record['id'])
                if head['id'] in head_ids:
                    history.setdefault(head['id'], []).append((record, commit))
        for root_id, row in chain_root.items():
            record, status, run = row['record'], row['status'], row['run'] or {}
            tid, pid = record['id'], record['project_id']
            task_node = f'task:{tid}'
            kind = self._attention_kind(status)
            source = run.get('source') or {}
            initiator = source.get('actor')
            providers = sorted({(e.get('payload') or {}).get('provider') for e in self._run_events(run.get('id'))
                                if e.get('type') == 'usage.recorded' and (e.get('payload') or {}).get('provider')}) if run else []
            add({'id': task_node, 'type': 'task', 'label': record['issue']['title'][:60], 'full_label': record['issue']['title'],
                 'sublabel': ('合成 · ' if record.get('synthetic') else '')
                 + f"第 {record['revision']} 修订 · {_STATUS_TEXT.get(status, status)}",
                 'status': status, 'task_id': tid, 'project_id': pid, 'synthetic': bool(record.get('synthetic')),
                 'tone': 'attention' if kind in ('answer', 'approval') else 'blocked' if kind == 'blocked'
                 else 'ok' if status in _DELIVERED else 'none' if status in ('cancelled', 'discarded') else 'pending',
                 'facts': [{'label': '基线版本', 'value': str(record.get('base_sha') or '未记录')[:12]},
                           {'label': '修订', 'value': f"第 {record['revision']} 修订"},
                           {'label': '状态', 'value': _STATUS_TEXT.get(status, status)},
                           {'label': '执行发起人', 'value': initiator or ('尚未执行' if not run else '未记录')},
                           {'label': '当前可处理', 'value': (f'发起人 {initiator} 或管理员' if initiator else '管理员')
                            if kind else '无需人工处理'},
                           {'label': '负责人', 'value': unrecorded},
                           {'label': '执行器', 'value': '、'.join(providers) or '未记录'},
                           {'label': '任务 ID', 'value': tid}],
                 'links': [{'kind': 'task', 'id': tid, 'label': '任务现场'}]},
                f'req:{requirement_of[root_id]}' if root_id in requirement_of else f'repo:{pid}', 'creates')
            for old, commit in history.get(tid, []):
                name = f"maintenance-{old['execution_id']}.patch"
                add({'id': f'artifact:{old["id"]}:{name}', 'type': 'artifact', 'label': f"补丁包（第 {old['revision']} 修订）",
                     'sublabel': f'已生成 · commit {commit[:10]}', 'status': 'generated', 'task_id': old['id'],
                     'project_id': pid, 'tone': 'ok',
                     'facts': [{'label': 'commit', 'value': commit}, {'label': '说明', 'value': '已生成 ≠ 已验收 ≠ 已上线'}],
                     'links': [{'kind': 'task', 'id': old['id'], 'label': '该修订的任务现场'}]}, task_node, 'produces')
            last = task_node
            if run:
                evidence = current_evidence(run)
                plan = run.get('plan') if isinstance(run.get('plan'), dict) else None
                plan_state = 'awaiting_approval' if status in _APPROVAL else ('executing' if evidence['execution'] else
                                                                                 'generated' if plan else 'none')
                add({'id': f'plan:{tid}', 'type': 'plan',
                     'label': (plan or {}).get('title') or '尚无方案', 'full_label': (plan or {}).get('title') or '尚无方案',
                     'sublabel': {'awaiting_approval': '等待批准', 'executing': '已进入执行', 'generated': '方案已生成',
                                  'none': '尚无记录'}[plan_state],
                     'status': plan_state, 'task_id': tid, 'project_id': pid,
                     'tone': {'awaiting_approval': 'attention', 'none': 'none'}.get(plan_state, 'ok'),
                     'facts': [{'label': '方案', 'value': (plan or {}).get('title') or '尚无记录'},
                               {'label': '步骤数', 'value': str(len(plan.get('tasks') or [])) if plan and plan.get('tasks') else '未记录'},
                               {'label': '批准', 'value': '等待批准' if plan_state == 'awaiting_approval' else
                                '尚无方案' if plan_state == 'none' else '批准记录见任务现场（此处不推断审批人）'}],
                     'links': [{'kind': 'task', 'id': tid, 'label': '在任务现场查看方案'}]}, task_node, 'plans')
                artifacts = run.get('artifacts') or {}
                checks = [c for c in (artifacts.get('checks') or []) if isinstance(c, dict)]
                check_state = evidence['checks']
                add({'id': f'check:{tid}', 'type': 'check',
                     'label': {'passed': '检查通过', 'failed': '检查未通过', 'recorded': '检查已记录',
                               'unverified': '未验证', 'none': '尚无检查记录'}.get(check_state, check_state),
                     'sublabel': f'{len(checks)} 条检查记录' if checks else '没有检查结果',
                     'status': check_state, 'task_id': tid, 'project_id': pid,
                     'tone': {'passed': 'ok', 'failed': 'blocked', 'none': 'none', 'unverified': 'attention'}.get(check_state, 'pending'),
                     'facts': ([{'label': str(c.get('name') or '检查'), 'value': '通过' if c.get('exit') == 0 else
                                 f"未通过（退出码 {c.get('exit')}）" if c.get('exit') is not None else '未完成'} for c in checks[:8]]
                               or [{'label': '检查', 'value': '尚无记录（项目未配置检查命令或尚未执行到检查）'}]),
                     'links': [{'kind': 'task', 'id': tid, 'label': '在任务现场查看检查输出'}]}, f'plan:{tid}', 'verified_by')
                last = f'check:{tid}'
                if kind == 'approval':
                    blocker_parent = f'plan:{tid}'
                else:
                    blocker_parent = task_node
            else:
                blocker_parent = task_node
            commit = (run.get('artifacts') or {}).get('commit') if run else None
            if status in _DELIVERED and commit:
                name = f"maintenance-{record['execution_id']}.patch"
                add({'id': f'artifact:{tid}:{name}', 'type': 'artifact', 'label': '补丁包',
                     'sublabel': f'已生成 · commit {commit[:10]}', 'status': 'generated', 'task_id': tid, 'project_id': pid,
                     'tone': 'ok',
                     'facts': [{'label': 'commit', 'value': commit}, {'label': '产物', 'value': name},
                               {'label': '说明', 'value': '已生成 ≠ 已验收 ≠ 已上线'}],
                     'links': [{'kind': 'task', 'id': tid, 'label': '在任务现场导出补丁'}]}, last, 'produces')
            elif status not in ('cancelled', 'discarded'):
                add({'id': f'target:{tid}', 'type': 'target', 'label': '目标：补丁包', 'sublabel': '尚未生成，不计入交付',
                     'status': 'pending', 'task_id': tid, 'project_id': pid, 'tone': 'none',
                     'facts': [{'label': '交付', 'value': '尚未生成'}], 'links': []}, last, 'targets')
            if kind:
                reason = self._reason(row, kind)
                add({'id': f'blocker:{tid}', 'type': 'blocker',
                     'label': {'answer': '等待业务回答', 'approval': '等待批准', 'blocked': '执行受阻'}[kind],
                     'sublabel': reason[:60], 'status': kind, 'task_id': tid, 'project_id': pid,
                     'tone': 'blocked' if kind == 'blocked' else 'attention',
                     'facts': [{'label': '原因', 'value': reason},
                               {'label': '可处理', 'value': (f'发起人 {initiator} 或管理员' if initiator else '管理员')}],
                     'links': [{'kind': 'task', 'id': tid, 'label': '去处理'}]}, blocker_parent, 'blocked_by')
        return {'generated_at': now(), 'nodes': nodes, 'edges': edges, 'truncated': truncated,
                'projects': [{'id': pid, 'name': projects[pid]['name']} for pid in scope]}

    def _run_events(self, run_id):
        if not run_id:
            return []
        from factory.control.autonomy import all_events
        try:
            return all_events(self.store, run_id)
        except Exception:  # noqa: BLE001 - unreadable events mean "not recorded", not a failure
            return []

    # -- host manifest -------------------------------------------------------
    @staticmethod
    def manifest() -> dict:
        return {
            'id': 'webuddy-maintenance', 'name': 'webuddy 运维维护子系统', 'version': VERSION,
            'contract_version': CONTRACT_VERSION, 'plugin_id': PLUGIN_ID,
            'entries': {'ui': {'monitor': '/maintenance', 'repos': '/maintenance/repos',
                               'intake': '/maintenance/intake', 'task': '/maintenance/{task_id}',
                               'embed': '/embed/maintenance'},
                        'api_prefix': '/api/v2/maintenance',
                        'cli': 'webuddy-maintenance'},
            'actions': ['answer', 'approve', 'supplement', 'resume', 'cancel', 'feedback', 'export'],
            'supports_pause': False,
            'intake': {'manual': 'POST /api/v2/maintenance/requirements（会话）',
                       'machine': 'POST /api/v2/maintenance/intake（Bearer 接入令牌）',
                       'idempotency': '来源 + 项目 + external_id'},
            'host_ports': {'identity': '宿主解析的用户身份（会话 / 本地 OS 用户 / 接入令牌）',
                           'project_authorization': '宿主项目权限（governance.require_project）',
                           'executor': '宿主已配置的 Claude Code / Codex 运行时（不携带凭据）',
                           'data_dir': 'FACTORY_CONTROL_DATA（control.db 所在目录）',
                           'workspace_root': 'FACTORY_WORKSPACE_ROOT'},
            'not_connected': ['server_metrics', 'app_probes', 'alerts'],
            'embed': {'component': 'EmbeddedMaintenance（frontend/src/maintenance/EmbeddedMaintenance.tsx）',
                      'route': '/embed/maintenance/*', 'same_origin_required': True},
        }


def executor_status(svc, store) -> dict:
    """Who, if anyone, holds the executor lock for this database."""
    queue = getattr(svc, 'queue', None)
    if queue is not None and getattr(queue, 'handle', None) is not None:
        return {'status': 'online', 'detail': '本进程持有执行器锁'}
    path = Path(store.path).with_suffix('.worker.lock')
    if not path.exists():
        return {'status': 'offline', 'detail': '没有进程启动过执行器'}
    try:
        with path.open('a+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'online', 'detail': '另一个进程持有执行器锁'}
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return {'status': 'unknown', 'detail': '无法读取执行器锁'}
    return {'status': 'offline', 'detail': '没有进程持有执行器锁，新任务会排队等待'}


def task_execution_owner(store, task_id) -> tuple:
    """``(actor_id, project_id)`` of whoever started this maintenance task's execution.

    The same identity the run-action rule checks (``runs.source.actor_id``). A task
    with no execution yet has no owner a member could match, so it answers
    ``(None, project_id)`` and the caller refuses. Unknown task ids raise
    ``KeyError`` (404), exactly as an unknown run does.
    """
    from factory.control.issue_maintenance import MaintenanceStore
    record = MaintenanceStore(store).get(task_id)
    execution_id = record.get('execution_id')
    if not execution_id:
        return None, record['project_id']
    run = store.get(execution_id)
    return (run.get('source') or {}).get('actor_id'), record['project_id']


def actions_for_user(store, view, actions, user) -> list:
    """The actions this signed-in user may actually perform, matching the gateway.

    Admins keep everything. A member keeps the write actions only on a task whose
    execution they started (and ``export``, a read, everywhere they can see the
    task) -- so the page never offers a button the gateway will answer with 403.
    """
    if user is None or user.get('role') == 'admin':
        return actions
    try:
        owner_id, _ = task_execution_owner(store, view['task_id'])
    except KeyError:
        owner_id = None
    if owner_id == user.get('id'):
        return actions
    return [a for a in actions if a == 'export']


_STATUS_TEXT = {'received': '已接收', 'queued': '排队中', 'planning': '制定计划', 'running': '执行中',
                'verifying': '检查中', 'requirement_analysis': '需求分析', 'needs_clarification': '等待回答',
                'awaiting_approval': '等待批准', 'awaiting_spec_confirmation': '等待确认',
                'needs_human': '执行受阻', 'failed': '失败', 'cancelled': '已取消',
                'ready_for_review': '产物就绪', 'published': '已发布', 'discarded': '已丢弃',
                'missing': '执行记录缺失'}
