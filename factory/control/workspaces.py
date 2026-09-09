"""Provision a managed workspace before any requirements or model calls exist."""
from __future__ import annotations
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from factory.control.store import Conflict, now


class WorkspaceError(Exception):
    pass


def initialize_repository(path: Path):
    for args in (['init', '--template=', '-q', '-b', 'main'],
                 ['config', 'user.name', 'Factory'],
                 ['config', 'user.email', 'factory@localhost.invalid'],
                 ['-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                  'commit', '--allow-empty', '-qm', 'Initialize managed workspace']):
        subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, timeout=15)


def create_workspace(store, root: Path, *, name: str, budget_usd: float, actor_id: int, idempotency_key: str):
    fingerprint = hashlib.sha256(json.dumps([name, budget_usd], ensure_ascii=False).encode()).hexdigest()
    owned = None
    try:
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workspace_creations(actor_id INTEGER NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, project_id TEXT NOT NULL, PRIMARY KEY(actor_id,request_key))')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT fingerprint,project_id FROM workspace_creations WHERE actor_id=? AND request_key=?', (actor_id, idempotency_key)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('这次创建请求的内容已改变，请重新打开新建工作区后提交')
                row = db.execute('SELECT data FROM projects WHERE id=?', (previous['project_id'],)).fetchone()
                if not row:
                    raise Conflict('已创建的工作区记录不可用，请刷新项目列表')
                return store._project_view(json.loads(row['data']))
            root.mkdir(parents=True, exist_ok=True)
            key = uuid.uuid4().hex
            path = root / ('workspace-' + key)
            path.mkdir(mode=0o700)
            owned = path
            initialize_repository(path)
            project = store._insert_project(db, {
                'name': name, 'repository': 'local/workspace-' + key, 'workspace': str(path),
                'base_branch': 'main', 'budget_usd': budget_usd,
                'checks': {'workspace-integrity': ['git', 'diff', '--check', 'HEAD']},
                'auto_issues': False, 'auto_publish': False, 'managed_workspace': True,
                'actor': str(actor_id),
            })
            db.execute('INSERT INTO workspace_creations VALUES(?,?,?,?)', (actor_id, idempotency_key, fingerprint, project['id']))
        return project
    except Exception as exc:
        # Remove only the fresh directory created by this attempt, never a user's repository.
        if owned is not None:
            shutil.rmtree(owned)
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise WorkspaceError('工作区准备失败，请稍后重试；系统管理员可检查工程目录写入权限和 Git 是否可用。') from None
        raise
