"""Explicit repository binding and durable, admin-initiated publication intent."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from factory.control.store import Conflict, PROJECT_EDIT_BLOCKING, now


class GitHubPublication:
    def __init__(self, service):
        self.service, self.store = service, service.store
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS github_publication_requests(run_id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def _publisher(self):
        if not self.service.publisher:
            raise Conflict('尚未配置 GitHub 发布凭据；请联系管理员配置，成果仍可下载。')
        return self.service.publisher

    def options(self, rid, page=1):
        run = self.store.get(rid)
        project = self.store.project(run['project_id'])
        result = {'github_configured': bool(self.service.publisher), 'account': None,
            'repositories': [], 'page': page, 'has_more': False,
            'github_repository': project['repository'],
            'github_repository_bound': not project['repository'].startswith('local/'),
            'project_revision': project['revision'],
            'repository_url': run.get('artifacts', {}).get('repository_url'),
            'publication_type': run.get('artifacts', {}).get('publication_type')}
        if self.service.publisher:
            result.update(account=self.service.publisher.account(), **self.service.publisher.repositories(page=page))
        return result

    def _editable(self, db, rid, project, revision, target_repository=None):
        if project.get('revision', 1) != revision:
            raise Conflict('项目设置已更新，请刷新成果页后重试')
        if rid in self.service.active_jobs:
            raise Conflict('成果现场仍在保存，请稍后发布')
        other_active = [active_rid for active_rid in self.service.active_jobs if active_rid != rid]
        if other_active:
            active_same_project = db.execute(
                "SELECT 1 FROM runs WHERE id IN (%s) "
                "AND json_extract(data, '$.project_id')=? LIMIT 1" %
                ','.join('?' for _ in other_active),
                (*other_active, project['id'])).fetchone()
            if active_same_project:
                raise Conflict('项目还有正在收尾的运行，暂时不能更换 GitHub 仓库')
        current_repository = project.get('repository', '')
        first_binding = current_repository.startswith('local/')
        same_repository = (isinstance(target_repository, str)
            and current_repository.casefold() == target_repository.casefold())
        blocking_statuses = (PROJECT_EDIT_BLOCKING - {'ready_for_review'}
                             if target_repository is not None and (first_binding or same_repository)
                             else PROJECT_EDIT_BLOCKING)
        busy = db.execute("SELECT 1 FROM runs WHERE id<>? AND json_extract(data, '$.project_id')=? "
            "AND json_extract(data, '$.status') IN (%s) LIMIT 1" % ','.join('?' for _ in blocking_statuses),
            (rid, project['id'], *sorted(blocking_statuses))).fetchone()
        if busy:
            raise Conflict('项目还有进行中或待交付任务，暂时不能更换 GitHub 仓库')

    def bind(self, rid, repository, revision, actor):
        metadata = self._publisher().repository(repository)
        return self._bind(rid, metadata, revision, actor)

    def _bind(self, rid, metadata, revision, actor):
        with self.service.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = json.loads(db.execute('SELECT data FROM runs WHERE id=?', (rid,)).fetchone()[0])
            if run['status'] != 'ready_for_review':
                raise Conflict('请先完成验证，再选择 GitHub 仓库')
            project = json.loads(db.execute('SELECT data FROM projects WHERE id=?', (run['project_id'],)).fetchone()[0])
            repository = metadata['full_name']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or repository.startswith('local/'):
                raise ValueError('GitHub 仓库名称无效')
            self._editable(db, rid, project, revision, repository)
            for row in db.execute('SELECT id,data FROM projects WHERE id<>?', (project['id'],)):
                if json.loads(row['data']).get('repository', '').casefold() == repository.casefold():
                    raise Conflict('该 GitHub 仓库已绑定其他项目')
            changes = {'repository': repository, 'github_repository_id': metadata['id'],
                'github_base_branch': metadata.get('default_branch') or 'main'}
            if all(project.get(k) == value for k, value in changes.items()):
                return self.store._project_view(project)
            updated = {**project, **changes, 'revision': revision + 1, 'updated_at': now()}
            db.execute('UPDATE projects SET data=? WHERE id=?', (json.dumps(updated), project['id']))
            db.execute('INSERT INTO project_settings_audit(project_id,revision,actor,action,data,at) VALUES (?,?,?,?,?,?)',
                (project['id'], updated['revision'], actor, 'github.bound',
                 json.dumps({**changes, 'previous_repository': project['repository'], 'run_id': rid}), now()))
            self.store._event(db, rid, 'github.repository_bound', changes)
            return updated

    def _receipt(self, rid):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM github_publication_requests WHERE run_id=?', (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, rid, receipt):
        with self.store.connect() as db:
            db.execute('INSERT INTO github_publication_requests VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET data=excluded.data',
                       (rid, json.dumps({**receipt, 'updated_at': now()})))

    def publish(self, rid, *, mode, repository=None, name=None, private=True, expected_project_revision, actor):
        publisher = self._publisher()
        if mode not in ('existing', 'create', 'bound') or private is not True:
            raise ValueError('请选择已有仓库或创建个人私有仓库')
        run = self.store.get(rid)
        project = self.store.project(run['project_id'])
        if mode == 'create':
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name):
                raise ValueError('请输入有效的 GitHub 仓库名称')
            target = publisher.account()['login'] + '/' + name
        else:
            target = project['repository'] if mode == 'bound' else repository
            if not isinstance(target, str) or target.startswith('local/') or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', target):
                raise ValueError('请先选择 GitHub 仓库')
        # Hold the coordinator lock over this explicit admin operation. The
        # durable receipt guards retries after restart as well as double clicks.
        with self.service.lock:
            run = self.store.get(rid)
            receipt = self._receipt(rid)
            if receipt and receipt['target'].casefold() != target.casefold():
                raise Conflict('本次成果已有另一仓库发布记录，请先核对原目标')
            if run['status'] == 'published':
                if run.get('artifacts', {}).get('repository', '').casefold() == target.casefold():
                    return run
                raise Conflict('本次成果已发布，请查看已有发布结果')
            if run['status'] != 'ready_for_review':
                raise Conflict('请先完成验证，再发布成果')
            project = self.store.project(run['project_id'])
            # Recover a crash after atomic project binding but before receipt
            # acknowledgement, only for the exact revision and repository id.
            if (receipt and not receipt.get('bound_revision') and receipt.get('repository')
                    and project['revision'] == receipt.get('expected_project_revision', -2) + 1
                    and project.get('github_repository_id') == receipt['repository']['id']
                    and project['repository'].casefold() == target.casefold()):
                receipt['bound_revision'] = project['revision']
                self._save(rid, receipt)
            resume_bound = bool(receipt and receipt.get('bound_revision') == project['revision']
                and project['repository'].casefold() == target.casefold())
            # A durable binding receipt lets a retry use the post-binding
            # revision, but it never bypasses project activity checks.
            editable_revision = project['revision'] if resume_bound else expected_project_revision
            with self.store.connect() as db:
                self._editable(db, rid, project, editable_revision, target)
            if receipt and receipt.get('state') == 'creating' and mode == 'create':
                raise Conflict('上次创建仓库的结果尚未确认；请刷新仓库列表并选择该已有仓库继续，系统不会重复创建')
            receipt = receipt or {'target': target, 'mode': mode, 'state': 'prepared', 'created_at': now(),
                'expected_project_revision': expected_project_revision}
            if not receipt.get('repository'):
                if mode == 'create':
                    receipt['state'] = 'creating'
                    self._save(rid, receipt)
                    # Leave 'creating' on an uncertain error. Never blindly POST twice.
                    metadata = publisher.create_repository(name=name, private=True)
                else:
                    metadata = publisher.repository(target)
                receipt.update(repository=metadata, state='prepared')
                self._save(rid, receipt)
            metadata = receipt['repository']
            if not resume_bound:
                project = self._bind(rid, metadata, expected_project_revision, actor)
                receipt['bound_revision'] = project['revision']
                self._save(rid, receipt)
            try:
                result = self.service.publish(rid)
            except Exception:
                receipt['state'] = 'failed'
                self._save(rid, receipt)
                raise
            receipt['state'] = 'published'
            self._save(rid, receipt)
            return result

    def sync_initial_baseline(self, run):
        """Only fast-forward a clean unchanged local baseline after initial upload."""
        if run.get('artifacts', {}).get('publication_type') != 'initial':
            return None
        artifacts = run['artifacts']
        project = self.store.project(run['project_id'])
        def git(root, *args):
            return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=root,
                check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        try:
            with self.service.lock, self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._editable(db, run['id'], project, project['revision'])
                root, worktree = project['workspace'], artifacts['worktree']
                common = lambda path: (Path(path) / git(path, 'rev-parse', '--git-common-dir')).resolve()
                if common(root) != common(worktree):
                    raise ValueError('成果工作区与项目不属于同一仓库')
                if git(root, 'status', '--porcelain') or git(root, 'symbolic-ref', 'HEAD') != 'refs/heads/' + project['base_branch']:
                    raise ValueError('项目工作区有未保存改动或未位于主分支')
                head = git(root, 'rev-parse', 'HEAD')
                if head == artifacts['commit']:
                    return {'status': 'synced', 'commit': head}
                if head != artifacts['base_sha']:
                    raise ValueError('项目基线已变化')
                git(root, 'merge', '--ff-only', '--no-edit', artifacts['commit'])
                return {'status': 'synced', 'commit': artifacts['commit']}
        except Exception:
            return {'status': 'needs_sync', 'message': 'GitHub 已发布；本地项目基线未自动推进，请保存本地改动并同步后再开启新任务。'}
