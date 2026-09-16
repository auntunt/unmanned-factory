import hashlib
import io
import json
import zipfile

import pytest

from factory.control.agents import AgentStore, inspect_skill
from factory.control.agent_manifests import ManifestStore
from factory.control.manifest_packs import export_pack, import_pack
from factory.control.mounts import compile_mounts, ReferenceTools
from tests.test_workbench_app import app_env


def prepared(store, *, reference='规定单位为 mm', windows=False, junk=False):
    agents = AgentStore(store)
    aid = agents.create({'name': '逆向工程师'}, 'owner')['id']
    body = 'Read `references/format.md` and [details](references/more.txt).'
    entry = '---\nname: reverse\ndescription: reverse formats\n---\n' + body
    raw = io.BytesIO()
    paths = {'reverse/SKILL.md': entry, 'reverse/references/format.md': reference,
             'reverse/references/more.txt': 'More details [next](next.md)',
             'reverse/references/next.md': 'Nested evidence',
             'other/SKILL.md': 'UNSELECTED', 'other/references/secret.md': 'NOT AUTHORIZED',
             'reverse/scripts/run.py': 'raise RuntimeError("MUST NOT EXECUTE")'}
    if junk:
        paths.update({'__MACOSX/._SKILL.md': 'junk', '.git/config': 'junk', 'node_modules/foo/index.js': 'junk'})
    with zipfile.ZipFile(raw, 'w') as archive:
        for path, text in paths.items():
            archive.writestr(path.replace('/', '\\') if windows else path, text)
    raw = raw.getvalue()
    sid = 'external_asset'
    source = {'asset_id': sid, 'agent_id': aid, 'path': 'reverse\\SKILL.md' if windows else 'reverse/SKILL.md',
              'package_sha256': hashlib.sha256(raw).hexdigest(), 'sha256': hashlib.sha256(entry.encode()).hexdigest(),
              'body_sha256': hashlib.sha256(body.encode()).hexdigest()}
    skill = {'id': 'external_module', 'version': 1, 'name': 'Reverse', 'category': 'workflow',
             'instructions': body, 'external_source': source, 'owner_agent_id': aid}
    with store.connect() as db:
        db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)', (sid, aid, json.dumps(inspect_skill(raw)), raw, 'now'))
        db.execute('INSERT INTO instruction_modules VALUES(?,?,?)', (skill['id'], 1, json.dumps(skill)))
    manifests = ManifestStore(store)
    initial = manifests.get(aid)
    manifests.save(aid, {'identity': 'Reverse', 'skills': [{'id': skill['id'], 'version': 1}], 'assertions': []}, initial['revision'], 'owner')
    run = {'project_id': 'project', 'agent_id': aid, 'agent_snapshot': manifests.freeze(aid, agents.version(aid))}
    return agents, manifests, aid, run


@pytest.mark.parametrize('windows', [False, True])
def test_signed_selected_external_references_are_readable_and_frozen(app_env, windows):
    _, store, _, _ = app_env
    agents, manifests, aid, run = prepared(store, windows=windows)
    mounted = compile_mounts(store, run)
    tools = ReferenceTools(mounted)
    assert tools.read('skill/external_asset/reverse/references/format.md')['text'] == '规定单位为 mm'
    assert tools.read('skill/external_asset/reverse/references/next.md')['text'] == 'Nested evidence'
    assert len(mounted['documents']) == 4
    assert tools.search('UNSELECTED')['total'] == 0
    with pytest.raises(ValueError):
        tools.read('skill/external_asset/other/references/secret.md')
    current = manifests.get(aid)
    manifests.save(aid, {'identity': 'Changed', 'skills': [], 'assertions': []}, current['revision'], 'owner')
    assert compile_mounts(store, run)['digest'] == mounted['digest']
    assert not compile_mounts(store, {**run, 'agent_snapshot': manifests.freeze(aid, agents.version(aid))})['documents']


def test_external_reference_pack_roundtrip_remaps_owned_assets(app_env):
    _, store, _, _ = app_env
    agents, manifests, aid, run = prepared(store, windows=True)
    for _ in range(2):
        imported = import_pack(manifests, export_pack(manifests, agents, aid), 'owner')
        aid = imported['id']
        snapshot = manifests.freeze(aid, agents.version(aid))
        source = snapshot['manifest_skills'][0]['external_source']
        assert source['asset_id'] != 'external_asset'
        assert source['agent_id'] == aid
        mounted = compile_mounts(store, {**run, 'agent_id': aid, 'agent_snapshot': snapshot})
        assert ReferenceTools(mounted).search('规定单位')['total'] == 1
        assert len(mounted['documents']) == 4


@pytest.mark.parametrize('change,error', [('owner', PermissionError), ('package', ValueError), ('body', ValueError), ('entry', ValueError)])
def test_external_references_reject_foreign_or_tampered_sources(app_env, change, error):
    _, store, _, _ = app_env
    _, _, _, run = prepared(store)
    skill = run['agent_snapshot']['manifest_skills'][0]
    if change == 'owner': skill['external_source']['agent_id'] = 'other'
    if change == 'package': skill['external_source']['package_sha256'] = '0' * 64
    if change == 'body': skill['instructions'] += 'changed'
    if change == 'entry': skill['external_source']['path'] = '../secret.md'
    with pytest.raises(error): compile_mounts(store, run)


def test_external_reference_size_limit_is_explicit(app_env):
    _, store, _, _ = app_env
    _, _, _, run = prepared(store, reference='x' * 40001)
    with pytest.raises(ValueError, match='40 KB'): compile_mounts(store, run)


def test_legacy_pack_missing_asset_id_recovers_by_signed_hash(app_env):
    _, store, _, _ = app_env
    agents, manifests, aid, run = prepared(store)
    exported = export_pack(manifests, agents, aid)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(exported)) as source, zipfile.ZipFile(out, 'w') as target:
        for info in source.infolist():
            raw = source.read(info)
            if info.filename == 'manifest.json':
                metadata = json.loads(raw)
                metadata['skill_metadata'][0]['external_source'].pop('asset_id')
                raw = json.dumps(metadata).encode()
            target.writestr(info, raw)
    imported = import_pack(manifests, out.getvalue(), 'owner')
    snapshot = manifests.freeze(imported['id'], agents.version(imported['id']))
    mounted = compile_mounts(store, {**run, 'agent_id': imported['id'], 'agent_snapshot': snapshot})
    assert ReferenceTools(mounted).search('规定单位')['total'] == 1


def test_bare_windows_references_resolve_and_do_not_execute_scripts(app_env):
    _, store, _, _ = app_env
    _, _, _, run = prepared(store, reference='Read next.md. Never run scripts/run.py')
    docs = compile_mounts(store, run)['documents']
    assert any(d['text'] == 'Nested evidence' for d in docs)
    assert not any(d['title'].endswith('.py') for d in docs)


def test_real_ingestion_sign_and_roundtrip_mounts_cleaned_zip(app_env, monkeypatch):
    from tests import test_skill_ingestion as ingestion_tests
    original = ingestion_tests.package
    def with_junk_and_reference():
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original())) as source, zipfile.ZipFile(out, 'w') as archive:
            for item in source.infolist():
                content = source.read(item)
                if item.filename == 'SKILL.md':
                    content += b'\nRead references/format.md\n'
                archive.writestr(item, content)
            archive.writestr('references/format.md', 'ACTUAL SIGNED REFERENCE')
            archive.writestr('__MACOSX/._SKILL.md', 'ignored metadata')
        return out.getvalue()
    monkeypatch.setattr(ingestion_tests, 'package', with_junk_and_reference)
    ingestion_tests.test_controlled_run_review_sign_and_authorization(app_env, monkeypatch)
    _, store, service, _ = app_env
    with store.connect() as db:
        aids = [row[0] for row in db.execute('SELECT id FROM agents')]
    assert len(aids) >= 2
    for aid in aids:
        snapshot = service.agent_manifests.freeze(aid, service.agents.version(aid))
        if not any(s.get('external_source') for s in snapshot['manifest_skills']):
            continue
        mounted = compile_mounts(store, {'project_id': 'project', 'agent_id': aid, 'agent_snapshot': snapshot})
        assert ReferenceTools(mounted).search('ACTUAL')['total'] == 1


def test_native_pack_import_cleans_inner_zip_and_preserves_source_hash_chain(app_env):
    _, store, _, _ = app_env
    agents, manifests, aid, run = prepared(store, junk=True)
    original_hash = run['agent_snapshot']['manifest_skills'][0]['external_source']['package_sha256']
    for _ in range(2):
        imported = import_pack(manifests, export_pack(manifests, agents, aid), 'owner')
        aid = imported['id']
        snapshot = manifests.freeze(aid, agents.version(aid))
        asset_id = snapshot['manifest_skills'][0]['external_source']['asset_id']
        with store.connect() as db:
            row = db.execute('SELECT data,content FROM skill_assets WHERE id=?', (asset_id,)).fetchone()
        meta, content = json.loads(row[0]), bytes(row[1])
        assert meta['source_sha256'] == original_hash
        assert meta['sha256'] == hashlib.sha256(content).hexdigest()
        assert meta['sha256'] != original_hash
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert not any(p.startswith(('__MACOSX/', '.git/', 'node_modules/')) for p in archive.namelist())
        mounted = compile_mounts(store, {**run, 'agent_id': aid, 'agent_snapshot': snapshot})
        assert ReferenceTools(mounted).search('规定单位')['total'] == 1
