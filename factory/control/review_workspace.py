"""Disposable, committed source snapshots for active independent verification."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import subprocess
import tempfile
import uuid


def source_fingerprints(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            result[str(path.relative_to(root))] = ('link', str(path.readlink()))
        elif path.is_file():
            result[str(path.relative_to(root))] = ('file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777)
    return result


def changed_sources(root, baseline):
    changed = []
    for name, expected in baseline.items():
        path = root / name
        # Check every ancestor: a replaced directory must not redirect reads.
        if any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
            changed.append(name)
            continue
        if path.is_symlink():
            actual = ('link', str(path.readlink()))
        elif path.is_file():
            actual = ('file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777)
        else:
            actual = None
        if actual != expected:
            changed.append(name)
    return changed


def preserve_screenshot(workspace, screenshot, evidence_root):
    """Persist only bounded local PNG evidence before the disposable tree dies."""
    root = Path(workspace).resolve()
    path = Path(screenshot)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError('验收截图不在现场内或超过大小限制')
    data = path.read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('验收截图不是 PNG')
    evidence_root = Path(evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = evidence_root / (uuid.uuid4().hex + '.png')
    with target.open('xb') as stream:
        target.chmod(0o600)
        stream.write(data)
    return str(target)


@contextmanager
def review_workspace(source, expected_commit=None):
    source = Path(source).resolve()
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True, timeout=15).strip()
    if expected_commit and commit != expected_commit:
        raise ValueError('验收提交与交付提交不一致')
    with tempfile.TemporaryDirectory(prefix='webuddy-review-') as directory:
        root = Path(directory) / 'workspace'
        root.mkdir()
        # Read Git blobs directly: archive honors export-ignore/export-subst,
        # which could silently omit tests or alter the source being verified.
        listing = subprocess.check_output(['git', '-C', str(source), 'ls-tree', '-rlz', commit], timeout=15)
        entries = [entry for entry in listing.split(b'\0') if entry]
        if len(entries) > 50_000:
            raise ValueError('验收快照超过 50000 个文件')
        parsed = []
        total_bytes = 0
        for entry in entries:
            metadata, name = entry.split(b'\t', 1)
            mode, kind, oid, size = metadata.decode().split()
            relative = Path(name.decode('utf-8'))
            if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
                raise ValueError('验收提交包含不安全路径')
            if kind != 'blob':
                raise ValueError('验收暂不支持未展开的 Git 子模块')
            total_bytes += int(size)
            if total_bytes > 300_000_000:
                raise ValueError('验收快照超过 300 MB')
            parsed.append((mode, oid, int(size), relative))
        # One batch process avoids one or two Git processes per source file.
        with tempfile.TemporaryFile() as blobs:
            subprocess.run(['git', '-C', str(source), 'cat-file', '--batch'],
                input=''.join(oid + '\n' for _, oid, _, _ in parsed).encode(),
                stdout=blobs, check=True, timeout=60)
            blobs.seek(0)
            for mode, oid, size, relative in parsed:
                header = blobs.readline().decode().strip()
                if header != f'{oid} blob {size}':
                    raise ValueError('验收源码读取不完整')
                data = blobs.read(size)
                if len(data) != size or blobs.read(1) != b'\n':
                    raise ValueError('验收源码读取不完整')
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if mode == '120000':
                    target = Path(data.decode('utf-8'))
                    if target.is_absolute() or not (path.parent / target).resolve().is_relative_to(root.resolve()):
                        raise ValueError('验收提交包含外部符号链接')
                    path.symlink_to(target)
                elif mode in ('100644', '100755'):
                    path.write_bytes(data)
                    path.chmod(0o755 if mode == '100755' else 0o644)
                else:
                    raise ValueError('不支持的验收文件类型')
        baseline = source_fingerprints(root)
        yield root, commit, baseline
