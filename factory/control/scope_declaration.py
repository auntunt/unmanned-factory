"""Append-only worker scope receipts and commit-pinned reconciliation; no execution permissions."""
import json
from pathlib import PurePosixPath
import re

from factory.control.spec_tree import SpecError, claims, git, safe_path


def baseline(run, artifacts=None):
    return (run.get('context') or {}).get('commit_sha') or (artifacts or run.get('artifacts') or {}).get('base_sha')


def declarations(store, rid):
    # export_events restores archived payloads, unlike the abbreviated live feed.
    return list(store.export_events(rid, kind='scope_declaration'))


_PLATFORM_PROGRESS_NOTE = '.webuddy/coding-progress.md'


def platform_browser_artifacts(store, rid, workspace, commit=None):
    """Screenshot files this run's platform browser wrote, whose COMMITTED BYTES
    are still the ones it produced.

    Two conditions, both required:

    1. The path was recorded on a browser.observed event of THIS run. The path is
       chosen by the platform bridge -- a random UUID under .webuddy/browser
       (runtime/project-browser/bridge.mjs::screenshotPath) -- never by the model.
    2. The content still matches what the platform bound to that path when it
       produced it. A recorded path on its own only proves the platform once
       wrote there; it does not prove the bytes that ended up in the commit. So
       the git blob id of the committed file must equal the one recorded at
       production time, which is exactly what `git rev-parse <commit>:<path>`
       returns -- and needs no binary decoding.

    Fails closed: events with no recorded binding (produced before this contract
    existed) are NOT exempt. We never reconstruct a past hash after the fact.
    """
    from pathlib import Path
    try:
        base = Path(workspace).resolve()
    except OSError:
        return frozenset()
    bound = {}
    for event in store.export_events(rid, kind='browser.observed'):
        payload = event.get('payload') or {}
        blob = payload.get('screenshot_blob')
        if not isinstance(blob, str) or not blob:
            continue  # no verifiable binding recorded: never exempt
        for key in ('screenshot_path', 'source_screenshot_path'):
            raw = payload.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                relative = Path(raw).resolve().relative_to(base)
            except (ValueError, OSError):
                continue  # outside the workspace, or unreadable
            bound.setdefault(relative.as_posix(), set()).add(blob)
    if not bound:
        return frozenset()
    verified = set()
    for path, blobs in bound.items():
        try:
            if commit:
                actual = git(workspace, 'rev-parse', '--verify', '--end-of-options',
                             f'{commit}:{path}').strip()
            else:
                data = (base / path).read_bytes()
                import hashlib
                actual = hashlib.sha1(b'blob %d\x00' % len(data) + data).hexdigest()
        except (SpecError, OSError):
            continue  # not present, or unreadable: not exempt
        if actual in blobs:
            verified.add(path)
    return frozenset(verified)


def exempt(path, workspace=None, platform_artifacts=frozenset()):
    name = PurePosixPath(path).name
    if path.startswith('.spec/') or bool(re.fullmatch(
            r'(?:test_.+\.py|.+_test\.py|.+\.(?:test|spec)\.(?:js|jsx|ts|tsx)|.+_test\.go)', name)):
        return True
    if path == _PLATFORM_PROGRESS_NOTE or path in platform_artifacts:
        if workspace is not None:
            from pathlib import Path
            full = Path(workspace) / path
            if full.is_symlink():
                return False
        return True
    return False


def changed(workspace, base, commit=None):
    base = git(workspace, 'rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}').strip()
    args = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only', '-z', base]
    if commit:
        args.append(git(workspace, 'rev-parse', '--verify', '--end-of-options', f'{commit}^{{commit}}').strip())
    paths = set(filter(None, git(workspace, *args, '--').split('\0')))
    if commit is None:
        paths.update(filter(None, git(workspace, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')))
    return paths


def payload(text):
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith('```json') and text.endswith('```'):
        text = text[7:-3].strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        # SDK text blocks may combine the worker's short introduction and its
        # standalone declaration. Accept one explicit object on its own line;
        # never mine tool output or ambiguous/multiple JSON objects.
        starts = list(re.finditer(r'^[ \t]*(?=\{)', text, re.MULTILINE))
        if not starts:
            return None
        start = starts[0].end()
        try:
            value, end = json.JSONDecoder().raw_decode(text[start:])
        except (ValueError, TypeError):
            return None
        outside = text[:start] + text[start + end:]
        if '{' in outside or '}' in outside:
            return None
    return value.get('scope_declaration') if isinstance(value, dict) else None


def wrap_emit(store, rid, request, emit):
    if request.read_only or request.verification:
        return emit
    run = store.get(rid)
    if not (run.get('spec_tree_enabled') or store.project(run['project_id']).get('spec_tree_enabled')):
        return emit
    base = baseline(run)

    def observe(kind, data=None, *extra):
        # Only normalized assistant messages count, never tool output or provider-supplied receipt events.
        if kind == 'scope_declaration':
            return
        emit(kind, data, *extra)
        declaration = payload(data.get('text')) if kind == 'assistant.message' and isinstance(data, dict) else None
        if declaration is None:
            return
        receipt = {'files': [], 'errors': [], 'baseline': base}
        try:
            entries = declaration.get('files') if isinstance(declaration, dict) else None
            if not isinstance(entries, list) or len(entries) > 2000:
                raise SpecError('files 必须为最多 2000 项的数组')
            if not base:
                raise SpecError('缺少运行基准提交，无法确认事前声明')
            already_changed = changed(request.workspace, base)
            prior = {f['path'] for e in declarations(store, rid) for f in e['payload'].get('files', []) if f.get('accepted')}
            for entry in entries:
                try:
                    if not isinstance(entry, dict): raise SpecError('文件项必须为对象')
                    path = safe_path(entry.get('path', ''))
                    if len(path) > 1024: raise SpecError('文件路径过长')
                    if path == '.' or path.endswith('/') or any(c in path for c in '*?[]#'):
                        raise SpecError('声明必须是精确文件路径，不允许目录、通配符或符号锚点')
                    path = str(PurePosixPath(path))
                    nodes = entry.get('spec_nodes', [])
                    if not isinstance(nodes, list) or len(nodes) > 100: raise SpecError('spec_nodes 必须为节点路径数组')
                    for node in nodes:
                        safe_path(node)
                        if not node.startswith('.spec/') or not node.endswith('/spec.md'): raise SpecError('无效规格节点路径')
                    late = path in already_changed and path not in prior
                    receipt['files'].append({'path': path, 'spec_nodes': nodes, 'accepted': not late, 'late': late})
                except (SpecError, TypeError, AttributeError) as exc:
                    receipt['errors'].append(str(exc))
        except (SpecError, OSError) as exc:
            receipt['errors'].append(str(exc))
        emit('scope_declaration', receipt)
    return observe


def evidence(store, rid, project, workspace, commit, artifacts=None):
    if not project.get('spec_tree_enabled'):
        return []
    run = store.get(rid)
    # Inspections have no worker and never change code: retain their existing read-only evidence.
    if run.get('source', {}).get('type') == 'inspection':
        return []
    item = {'id': 'scope:reconciliation', 'text': '执行范围声明与实际改动对账',
            'status': 'unverified', 'error_type': 'undeclared_changes', 'undeclared_changes': []}
    try:
        base = baseline(run, artifacts)
        if not base: raise SpecError('缺少运行基准提交，无法核对实际改动')
        actual = changed(workspace, base, commit)
        records = declarations(store, rid)
        declared = {f['path'] for e in records for f in e['payload'].get('files', []) if f.get('accepted')}
        produced = platform_browser_artifacts(store, rid, workspace, commit)
        excluded = {p for p in actual if exempt(p, workspace=workspace, platform_artifacts=produced)}
        undeclared = sorted(actual - excluded - declared)
        item.update(status='fail' if undeclared else 'pass', baseline=base, commit=commit,
                    actual_changes=sorted(actual), declared_files=sorted(declared), exempt_files=sorted(excluded),
                    declaration_event_ids=[e['id'] for e in records], undeclared_changes=undeclared)
        item['evidence'] = '未事前声明的改动：' + '、'.join(undeclared) if undeclared else '实际改动均已事前声明或属于明确豁免文件'
    except (SpecError, OSError) as exc:
        item['evidence'] = str(exc)
    return [item]


def recent(store, pid, node):
    if not node['code']:
        return []
    # Event order, rather than run creation order, includes resumed older runs.
    with store.connect() as db:
        rows = db.execute("SELECT e.id, e.run_id FROM events e JOIN runs r ON r.id=e.run_id "
                          "WHERE e.type='scope_declaration' AND json_extract(r.data, '$.project_id')=? "
                          "ORDER BY e.id DESC", (pid,)).fetchall()
    result, cache = [], {}
    for row in rows:
        rid = row['run_id']
        if rid not in cache:
            cache[rid] = (store.get(rid), {e['id']: e for e in declarations(store, rid)})
        run, events = cache[rid]
        e = events[row['id']]
        files = [f['path'] for f in e['payload'].get('files', [])
                 if any(claims(c['path'], f['path']) for c in node['code'])]
        if not files:
            continue
        checks = (run.get('artifacts') or {}).get('scope_reconciliation', [])
        status = checks[0]['status'] if checks else 'pending'
        result.append({'run_id': rid, 'at': e['at'], 'task_id': e.get('task_id'),
                       'event_id': e['id'], 'files': files, 'status': status})
        if len(result) == 20:
            break
    return result
