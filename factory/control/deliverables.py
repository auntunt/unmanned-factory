"""Immutable, run-scoped deliverables independent of GitHub publication."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from factory.control.store import Conflict

MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
TEXT = {'.txt', '.md', '.json', '.csv', '.py', '.js', '.ts', '.tsx', '.css', '.html', '.htm', '.yml', '.yaml', '.toml', '.xml', '.sh'}
IMAGES = {'.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
INSTALLERS = {'.exe', '.msi', '.dmg', '.pkg', '.apk', '.aab', '.deb', '.rpm', '.appimage'}


def safe_path(name):
    path = PurePosixPath(name)
    return bool(name) and '\\' not in name and ':' not in name and not any(ord(c) < 32 for c in name) and not path.is_absolute() and '..' not in path.parts and not any(
        part in {'.git', '.ssh', 'node_modules', '__pycache__', '.venv'} or
        part == '.env' or (part.startswith('.env.') and part not in {'.env.example', '.env.sample', '.env.template'}) or part.endswith(('.pem', '.key'))
        for part in path.parts)


def kind(name):
    suffix = Path(name).suffix.lower()
    if suffix in INSTALLERS: return 'installer'
    if suffix in {'.zip', '.gz', '.tar', '.whl'}: return 'package'
    if suffix in {'.html', '.htm'}: return 'web'
    if suffix in IMAGES: return 'image'
    if suffix in {'.pdf', '.docx', '.xlsx', '.pptx', '.md', '.txt', '.csv'}: return 'document'
    return 'source'


def snapshot(store, run):
    artifacts = run.get('artifacts') or {}
    root = Path(artifacts.get('worktree') or '/nonexistent').resolve()
    sha = artifacts.get('commit', '')
    if not (isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha)):
        raise Conflict('还没有可归档的验收版本，请先完成执行与验证。')
    def git(*args):
        try:
            return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            raise Conflict('成果工作区已不可用，请恢复工作区或重新执行。') from None
    if git('rev-parse', 'HEAD').decode().strip() != sha or git('status', '--porcelain', '--untracked-files=no').strip():
        raise Conflict('验收后代码已变化，请重新验证再保存成果。')
    target = Path(store.path).parent / 'deliverables' / run['id'] / sha
    if (target / 'manifest.json').exists():
        return json.loads((target / 'manifest.json').read_text())
    entries = []
    modes = {}
    for entry in git('ls-tree', '-r', '-z', sha).split(b'\0'):
        if not entry: continue
        meta, raw = entry.split(b'\t', 1)
        mode, typ, oid = meta.decode().split()
        name = raw.decode('utf-8')
        if typ == 'blob' and mode in {'100644', '100755'} and safe_path(name):
            entries.append((name, oid, None))
            modes[name] = int(mode, 8)
    # Build outputs need not be committed. Only conventional output directories
    # or explicitly declared relative files are eligible; never walk the repository.
    declared = []
    manifest_entry = next((e for e in entries if e[0] == '.factory-delivery.json'), None)
    if manifest_entry:
        try:
            config = json.loads(git('cat-file', 'blob', manifest_entry[1]))
            if not isinstance(config, dict): raise ValueError()
        except (ValueError, UnicodeError):
            raise Conflict('交付清单必须是包含 files 数组的 JSON 对象。') from None
        declared = config.get('files', [])
        if not isinstance(declared, list) or any(not isinstance(x, str) or not safe_path(x) for x in declared):
            raise Conflict('交付清单 files 必须是工作区内的相对文件路径。')
    else:
        for folder in ('dist', 'release', 'out'):
            base = root / folder
            if base.is_dir() and not base.is_symlink():
                for p in base.rglob('*'):
                    if p.is_file(): declared.append(p.relative_to(root).as_posix())
                    if len(declared) > 5000: raise Conflict('成果文件超过 5000 个，请用交付清单选择需要交付的文件。')
    seen = {e[0] for e in entries}
    for name in declared:
        p = root / name
        if not safe_path(name) or p.is_symlink() or any((root / parent).is_symlink() for parent in PurePosixPath(name).parents):
            continue
        if not p.resolve().is_relative_to(root.resolve()) or not p.is_file():
            raise Conflict('交付清单中存在缺失或越界的文件。')
        if name not in seen:
            entries.append((name, None, p)); seen.add(name)
    if len(entries) > 5000: raise Conflict('成果文件超过 5000 个，无法归档。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        temp = Path(temporary)
        items, total = [], 0
        with zipfile.ZipFile(temp / 'files.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, oid, p in entries:
                size = int(git('cat-file', '-s', oid)) if oid else p.stat().st_size
                total += size
                if size > MAX_FILE or total > MAX_TOTAL:
                    raise Conflict('成果超过归档上限（单文件 256 MB，总计 512 MB），请精简交付文件。')
                if oid:
                    content = git('cat-file', 'blob', oid)
                else:
                    # Bound the read even if a build process grows the file.
                    with p.open('rb') as source:
                        content = source.read(size + 1)
                if len(content) != size: raise Conflict('构建产物正在变化，请完成构建后重新保存。')
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = modes.get(name, 0o100644) << 16
                archive.writestr(info, content)
                items.append({'id': len(items), 'name': name, 'kind': kind(name), 'size': size,
                              'sha256': hashlib.sha256(content).hexdigest(),
                              'origin': 'commit' if oid else 'build',
                              'preview': (Path(name).suffix.lower() in TEXT or Path(name).suffix.lower() in IMAGES) and size <= 1024 * 1024})
        manifest = {'commit': sha, 'items': items, 'size': total,
                    'note': '源码来自验收提交；构建产物为归档时快照，不代表已签名或已安装验证。'}
        (temp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))
        try: temp.rename(target)
        except OSError:
            if not (target / 'manifest.json').exists(): raise
    return json.loads((target / 'manifest.json').read_text())


def router(store, service):
    api = APIRouter(prefix='/api/v3/runs/{rid}/deliverables')
    def location(rid):
        run = store.get(rid)
        sha = (run.get('artifacts') or {}).get('commit', '')
        if not (isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha)): return run, None
        return run, Path(store.path).parent / 'deliverables' / run['id'] / sha
    def manifest(rid):
        run, path = location(rid)
        if path is None or not (path / 'manifest.json').is_file():
            raise HTTPException(404, '尚未保存成果，请先保存本次成果。')
        return path, json.loads((path / 'manifest.json').read_text())
    @api.get('')
    def listing(rid: str):
        run, path = location(rid)
        saved = path is not None and (path / 'manifest.json').is_file()
        result = json.loads((path / 'manifest.json').read_text()) if saved else {'items': []}
        error = (run.get('artifacts') or {}).get('publish_error')
        if not error and run['status'] != 'published':
            with store.connect() as db:
                event = db.execute("SELECT type,payload FROM events WHERE run_id=? AND type IN ('github.publish_failed','github.published') ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
            if event and event['type'] == 'github.publish_failed':
                from factory.control.github import publish_failure_message
                error = publish_failure_message(ValueError(json.loads(event['payload']).get('message', '')))
        project = store.project(run['project_id'])
        recommended = None
        # Prefer the most recent screenshot actually recorded by the browser,
        # rather than the first UUID-sorted image or an empty JavaScript shell.
        with store.connect() as db:
            observations = db.execute('SELECT payload FROM events WHERE run_id=? AND type=? ORDER BY id DESC LIMIT 100',
                                      (rid, 'browser.observed')).fetchall()
        workspace = str((run.get('artifacts') or {}).get('worktree') or '').rstrip('/')
        for observation in observations:
            image = json.loads(observation['payload']).get('screenshot_path')
            if not isinstance(image, str) or not workspace or not image.startswith(workspace + '/'):
                continue
            name = image[len(workspace)+1:]
            candidate = next((item for item in result['items'] if item['name'] == name and item['kind'] == 'image' and item['preview']), None)
            if candidate:
                recommended = candidate['id']; break
        return {**result, 'recommended_preview_id': recommended, 'saved': saved, 'can_collect': run['status'] in ('ready_for_review', 'published'),
                'github_configured': bool(service.publisher),
                'github_repository': project['repository'],
                'github_repository_bound': not project['repository'].startswith('local/'),
                'project_revision': project['revision'],
                'repository_url': (run.get('artifacts') or {}).get('repository_url'),
                'publication_type': (run.get('artifacts') or {}).get('publication_type'),
                'baseline_sync': (run.get('artifacts') or {}).get('baseline_sync'),
                'publish_error': error,
                'collection_error': (run.get('artifacts') or {}).get('collection_error')}
    @api.post('/collect')
    def collect(rid: str):
        run = store.get(rid)
        if run['status'] not in ('ready_for_review', 'published'): raise Conflict('请先完成验证，再保存成果。')
        return snapshot(store, run)
    @api.get('/download')
    def download(rid: str):
        path, _ = manifest(rid)
        return FileResponse(path / 'files.zip', media_type='application/zip', filename=f'成果-{rid}.zip')
    @api.get('/files/{item_id}')
    def file(rid: str, item_id: int, preview: bool = False):
        path, data = manifest(rid)
        if item_id < 0 or item_id >= len(data['items']): raise HTTPException(404, '成果不存在')
        item = data['items'][item_id]
        if preview:
            if not item['preview']: raise HTTPException(415, '此文件请下载后查看')
            with zipfile.ZipFile(path / 'files.zip') as archive:
                raw = archive.read(item['name'])
                if item['kind'] == 'web':
                    from factory.control.static_preview import static_preview
                    try:
                        return {'name': item['name'], 'content': static_preview(archive, item['name']), 'kind': 'web'}
                    except ValueError as exc:
                        raise HTTPException(413, str(exc)) from None
            mime = IMAGES.get(Path(item['name']).suffix.lower())
            if mime:
                return {'name': item['name'], 'content': '', 'kind': 'image',
                        'image_url': f'data:{mime};base64,' + base64.b64encode(raw).decode()}
            return {'name': item['name'], 'content': raw.decode('utf-8', errors='replace'), 'kind': item['kind']}
        def stream():
            with zipfile.ZipFile(path / 'files.zip') as archive, archive.open(item['name']) as source:
                while chunk := source.read(1024 * 1024): yield chunk
        from urllib.parse import quote
        return StreamingResponse(stream(), media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(Path(item['name']).name)})
    return api
