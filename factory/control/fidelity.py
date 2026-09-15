"""Per-screen fidelity criteria require an independent judgment and real browser screenshots."""
import json


def criteria(run):
    target = run.get('fidelity_target') if run.get('spec_confirmation') else None
    if not target:
        return []
    return [{'id': f'fidelity:{screen_index}:{kind}:{index}', 'task_id': None,
        'class': 'fidelity', 'screen': screen['screen'], 'aspect': kind,
        'text': f"参照 {target['reference']} · {screen['screen']} · {kind}: {point}"}
        for screen_index, screen in enumerate(target['screens'])
        for kind in ('layout', 'colors', 'components', 'interactions')
        for index, point in enumerate(screen[kind])]


def prompt(run):
    if not criteria(run):
        return ''
    return ('\nFIDELITY ACCEPTANCE: For every fidelity: criterion, use project-browser to visit the matching local screen, '
        'exercise its core interactions, capture a screenshot, and read the image. Compare the actual screenshot '
        'against the owner-confirmed per-screen layout/colors/components/interactions, NOT merely whether the page runs. '
        'Do not fetch the external reference product. The reference is a model-knowledge/user-material specification, '
        'not a claim of pixel-perfect original screenshots. In each criterion row additionally return '
        'judgment:"like"|"unlike", screenshot_paths:[exact screenshot_path values returned by project-browser], and evidence explaining '
        'which screen/aspect matches or differs. unlike must have status=fail; missing browser evidence is unverified. '
        'Never invent screenshot paths. The platform resolves paths to its actual event IDs.\n' + json.dumps(run['fidelity_target'], ensure_ascii=False))


def enforce(store, rid, run, verdict, ledger):
    required = criteria(run)
    if not required:
        return verdict
    with store.connect() as db:
        since = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=? AND type='verification.workspace_created'", (rid,)).fetchone()[0]
        observations = db.execute("SELECT id,payload FROM events WHERE run_id=? AND task_id='verification' AND type='browser.observed' AND id>?", (rid, since)).fetchall()
    screenshots = {}
    source_paths = {}
    for event in observations:
        data = json.loads(event['payload'])
        if data.get('ok') and data.get('screenshot_path') and not data.get('error'):
            screenshots[event['id']] = data['screenshot_path']
            source_paths[data.get('source_screenshot_path') or data['screenshot_path']] = event['id']
    rows = {r.get('id'): r for r in verdict.get('criteria', []) if isinstance(r, dict)}
    for item in ledger['items']:
        if item.get('class') != 'fidelity':
            continue
        row = rows.get(item['id'], {})
        refs = row.get('screenshot_event_ids', [])
        paths = row.get('screenshot_paths', [])
        if isinstance(paths, list) and paths and all(isinstance(p, str) and p in source_paths for p in paths):
            refs = [source_paths[p] for p in paths]
        actual = refs if isinstance(refs, list) and refs and all(type(i) is int and i in screenshots for i in refs) else []
        item['judgment'] = row.get('judgment')
        item['screenshots'] = [{'event_id': i, 'path': screenshots[i]} for i in actual]
        if row.get('judgment') == 'unlike' or item['status'] == 'fail':
            item['status'] = 'fail'
            item['evidence'] = item['evidence'] or f"{item['screen']} 的 {item['aspect']} 不符合保真标尺"
        elif row.get('judgment') != 'like' or not actual or not item['evidence']:
            item['status'] = 'unverified'
            item['evidence'] = (item['evidence'] + '\n缺少本轮独立验收的逐屏截图或 like/unlike 判定').strip()
    ledger['counts'] = {s: sum(i['status'] == s for i in ledger['items']) for s in ('pass', 'fail', 'unverified')}
    ledger['complete'] = ledger['accounted'] and ledger['counts']['pass'] == ledger['total']
    failed = [i for i in ledger['items'] if i.get('class') == 'fidelity' and i['status'] == 'fail']
    missing = [i for i in ledger['items'] if i.get('class') == 'fidelity' and i['status'] == 'unverified']
    if failed:
        # A visual mismatch is a repairable implementation failure, not an
        # environment error that should skip coding on checkpoint continuation.
        return {**verdict, 'verdict': 'fail', 'error_type': None,
                'reason': '保真验收未通过：' + '；'.join(f"{i['screen']} / {i['aspect']}: {i['evidence']}" for i in failed)[:2500]}
    if missing and verdict['verdict'] != 'fail':
        return {**verdict, 'verdict': 'unverified', 'error_type': 'fidelity_unverified',
                'reason': '保真验收缺少可核对的逐屏截图与判断'}
    return verdict
