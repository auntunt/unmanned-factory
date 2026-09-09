"""Bounded project archive import. Uploaded code is never executed here."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import subprocess
import unicodedata
import uuid
import zipfile
import zlib
from pathlib import Path

from factory.control.store import Conflict, now
from factory.control.workspaces import WorkspaceError, initialize_repository

MAX_ARCHIVE = 20 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_FILES = 5000
_SECRET = re.compile(r'(?i)^(?:\.env(?:\..*)?|\.npmrc|\.netrc|id_rsa(?:\..*)?|.*\.(?:pem|key|p12|pfx))$')
_EXAMPLES = {'.env.example', '.env.sample', '.env.template'}
_RESERVED = {'.git', '.webuddy', '__macosx', 'node_modules', '.venv', 'venv', '__pycache__'}


def _excluded(parts):
    return any(p.casefold() in _RESERVED for p in parts) or any(_SECRET.match(p) and p.casefold() not in _EXAMPLES for p in parts)


class ImportError(ValueError):
    """An archive cannot safely become a managed project."""


def _members(archive):
    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise ImportError('压缩包文件数量超过 5000')
    total, names, result = 0, {}, []
    for info in entries:
        raw = info.orig_filename
        name = raw.rstrip('/')
        parts = name.split('/')
        mode = info.external_attr >> 16
        if (not name or len(name) > 1000 or any(ord(c) < 32 for c in raw)
                or '\\' in raw or ':' in raw or raw.startswith('/')
                or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)):
            raise ImportError('压缩包包含不安全的文件路径')
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ImportError('压缩包不能包含符号链接或特殊文件')
        if info.flag_bits & 1:
            raise ImportError('请上传未加密的 ZIP')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in names:
            raise ImportError('压缩包包含重复或大小写冲突的路径')
        names[key] = info.is_dir()
        total += info.file_size
        if total > MAX_EXPANDED or (info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 200):
            raise ImportError('压缩包展开体积或压缩比超过限制')
        result.append((info, parts))
    for key in names:
        parts = key.split('/')
        if any(names.get('/'.join(parts[:i])) is False for i in range(1, len(parts))):
            raise ImportError('压缩包中文件与目录路径冲突')
    return result


def _extract(archive, target, filename, digest):
    entries = _members(archive)
    all_files = [(i, p) for i, p in entries if not i.is_dir()]
    file_entries = [(i, p) for i, p in all_files if not _excluded(p)]
    if not file_entries:
        raise ImportError('压缩包没有项目文件')
    roots = {p[0] for _, p in file_entries}
    stripped = next(iter(roots)) if len(roots) == 1 and all(len(p) > 1 for _, p in file_entries) else None
    paths, excluded, total = [], len(all_files) - len(file_entries), 0
    for info, original in file_entries:
        parts = original[1:] if stripped else original
        path = target.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with archive.open(info) as source, path.open('xb') as destination:
            while chunk := source.read(65536):
                written += len(chunk)
                total += len(chunk)
                if total > MAX_EXPANDED or written > info.file_size:
                    raise ImportError('压缩包实际展开体积超过限制')
                destination.write(chunk)
        path.chmod(0o700 if info.external_attr >> 16 & 0o111 else 0o600)
        paths.append('/'.join(parts))
    if not paths:
        raise ImportError('压缩包未包含可导入的项目文件')
    manifests = {'package.json', 'pyproject.toml', 'requirements.txt', 'cargo.toml', 'go.mod', 'pom.xml', 'dockerfile', 'compose.yaml', 'docker-compose.yml', 'makefile'}
    entrypoints = {'main.py', '__main__.py', 'app.py', 'index.js', 'index.ts', 'main.go', 'main.rs', 'cli.py', 'server.py'}
    warnings = ['仅完成静态识别；尚未运行依赖安装、启动命令或功能验证。']
    if excluded:
        warnings.append(f'已排除 {excluded} 个 Git 内部文件、凭证、保留目录或生成依赖文件；原始 ZIP 已保留。')
    return {'filename': filename, 'sha256': digest, 'file_count': len(paths), 'total_bytes': total,
            'stripped_root': stripped, 'manifests': sorted(p for p in paths if Path(p).name.casefold() in manifests)[:100],
            'documents': sorted(p for p in paths if Path(p).suffix.casefold() in {'.md', '.rst', '.txt'} and Path(p).name.casefold() != 'requirements.txt')[:100],
            'entrypoints': sorted(p for p in paths if Path(p).name.casefold() in entrypoints)[:100],
            'warnings': warnings, 'baseline_status': 'not_run', 'report_path': '.webuddy/import-report.json'}


def import_project(store, root: Path, upload, *, filename, name, budget_usd, actor_id, idempotency_key, agent_id=None):
    upload.seek(0, 2)
    if upload.tell() > MAX_ARCHIVE:
        raise ImportError('ZIP 文件不能超过 20 MiB')
    upload.seek(0)
    digest = hashlib.sha256()
    while chunk := upload.read(65536):
        digest.update(chunk)
    sha = digest.hexdigest()
    upload.seek(0)
    fingerprint = hashlib.sha256(json.dumps([name, budget_usd, sha, agent_id], ensure_ascii=False).encode()).hexdigest()
    owned = evidence = None
    try:
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS project_imports(actor_id INTEGER NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, project_id TEXT NOT NULL, PRIMARY KEY(actor_id,request_key))')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT fingerprint,project_id FROM project_imports WHERE actor_id=? AND request_key=?', (actor_id, idempotency_key)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('这次导入请求的内容已改变，请重新提交')
                row = db.execute('SELECT data FROM projects WHERE id=?', (previous['project_id'],)).fetchone()
                if not row:
                    raise Conflict('已导入的项目记录不可用')
                project = store._project_view(json.loads(row['data']))
                return {'project': project, 'import_summary': project['import_summary']}
            root.mkdir(parents=True, exist_ok=True)
            key = uuid.uuid4().hex
            owned = root / ('workspace-' + key)
            owned.mkdir(mode=0o700)
            with zipfile.ZipFile(upload) as archive:
                summary = _extract(archive, owned, Path(filename or 'project.zip').name[:200], sha)
            (owned / '.webuddy').mkdir()
            (owned / summary['report_path']).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            # A private HOME prevents global clean filters, signing and attributes from
            # executing as a side effect of staging untrusted project contents.
            import tempfile, os
            with tempfile.TemporaryDirectory() as git_home:
                env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
                env.update(HOME=git_home, XDG_CONFIG_HOME=git_home, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
                initialize_repository(owned, env=env)
                for args in (['add', '-f', '--all'], ['-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Import project baseline']):
                    subprocess.run(['git', *args], cwd=owned, env=env, check=True, capture_output=True, timeout=30)
            evidence_root = root / '.imports'
            evidence_root.mkdir(mode=0o700, exist_ok=True)
            evidence = evidence_root / key
            evidence.mkdir(mode=0o700)
            upload.seek(0)
            with (evidence / 'original.zip').open('xb') as original:
                shutil.copyfileobj(upload, original, 65536)
            (evidence / 'original.zip').chmod(0o600)
            (evidence / 'report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            project = store._insert_project(db, {'name': name, 'repository': 'local/workspace-' + key,
                'workspace': str(owned), 'base_branch': 'main', 'budget_usd': budget_usd,
                'checks': {'workspace-integrity': ['git', 'diff', '--check', 'HEAD']},
                'auto_issues': False, 'auto_publish': False, 'managed_workspace': True,
                'actor': str(actor_id), 'import_summary': summary, 'import_evidence': str(evidence), 'imported_at': now()})
            db.execute('INSERT INTO project_imports VALUES(?,?,?,?)', (actor_id, idempotency_key, fingerprint, project['id']))
        return {'project': project, 'import_summary': summary}
    except Exception as exc:
        if owned is not None:
            shutil.rmtree(owned, ignore_errors=True)
        if evidence is not None:
            shutil.rmtree(evidence, ignore_errors=True)
        if isinstance(exc, (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError)):
            raise ImportError('ZIP 文件损坏或使用了不支持的压缩格式') from None
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise WorkspaceError('项目导入失败，请检查工作区存储和 Git 环境后重试') from None
        raise
