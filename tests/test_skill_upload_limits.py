"""Entry-specific ZIP limits, inert macOS filtering and actionable upload errors."""
import hashlib
import io
import zipfile

import pytest

from factory.control.agents import inspect_skill, MAX_PACK_FILES, MAX_FILE, strip_macos_junk
from factory.control.skill_ingestion import read_package
from factory.control.skill_ingestion_routes import directory_zip
from factory.control.agent_packs import pack_zip, catalog
from tests.test_control_app import app_env, login, project


def archive(count=636, junk=0, multiple=False):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('SKILL.md', '---\nname: root\ndescription: read only\n---\nRoot skill')
        for i in range(count - 1):
            z.writestr(f'files/{i}.txt', 'reference')
        if multiple:
            z.writestr('leaf/SKILL.md', 'Leaf skill')
        for i in range(junk):
            z.writestr(f'__MACOSX/._{i}.txt', 'junk')
        if junk:
            z.writestr('nested/.DS_Store', 'junk')
            z.writestr('nested/._SKILL.md', 'not a skill')
    return out.getvalue()


def test_file_count_is_per_channel_and_macos_members_are_ignored():
    raw = archive()
    with pytest.raises(ValueError, match='文件数超过 500'):
        inspect_skill(raw, max_files=500)
    assert len(inspect_skill(raw, max_files=MAX_PACK_FILES)['files']) == 636
    assert len(read_package(raw)['files']) == 636
    with pytest.raises(ValueError, match='文件数超过 3000'):
        inspect_skill(archive(3001), max_files=MAX_PACK_FILES)
    dirty = archive(count=500, junk=600)
    assert len(inspect_skill(dirty)['files']) == 500
    with zipfile.ZipFile(io.BytesIO(strip_macos_junk(dirty))) as z:
        assert len(z.namelist()) == 500


def test_single_file_size_gate_is_unchanged():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('large.bin', b'x' * (MAX_FILE + 1))
    for limit in (500, 3000):
        with pytest.raises(ValueError, match='单文件超过 2 MiB'):
            inspect_skill(out.getvalue(), max_files=limit)


def test_large_package_routes_and_clean_storage(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    monkeypatch.setattr(service, 'start_plan', lambda *_: None)
    agent = service.agents.create({'name':'Reader'}, 'owner')
    raw = archive(junk=600)
    attachment = client.post(f"/api/v4/agents/{agent['id']}/skills", headers=headers,
        files={'file':('large.zip',raw,'application/zip')})
    assert attachment.status_code == 400
    assert '文件数超过 500' in attachment.json()['detail']
    assert '适配与人签' in attachment.json()['detail']
    response = client.post('/api/v4/skill-ingestions',headers=headers,data={'project_id':p['id']},
        files={'file':('large.zip',raw,'application/zip')})
    assert response.status_code == 201, response.text
    iid = response.json()['id']
    assert response.json()['source_sha256'] == hashlib.sha256(raw).hexdigest()
    parsed = service.skill_ingestions.get(iid, package=True)
    assert parsed['sha256'] == hashlib.sha256(raw).hexdigest()
    assert len(parsed['files']) == 636
    with store.connect() as db:
        saved = db.execute('SELECT package FROM skill_ingestions WHERE id=?',(iid,)).fetchone()[0]
    with zipfile.ZipFile(io.BytesIO(saved)) as z:
        assert len(z.namelist()) == 636
    small = archive(2, junk=600)
    result = client.post(f"/api/v4/agents/{agent['id']}/skills",headers=headers,
        files={'file':('small.zip',small,'application/zip')})
    assert result.status_code == 201, result.text
    with store.connect() as db:
        saved = db.execute('SELECT content FROM skill_assets WHERE id=?',(result.json()['id'],)).fetchone()[0]
    with zipfile.ZipFile(io.BytesIO(saved)) as z:
        assert len(z.namelist()) == 2
    multiple = client.post(f"/api/v4/agents/{agent['id']}/skills",headers=headers,
        files={'file':('multiple.zip',archive(2,multiple=True),'application/zip')})
    assert multiple.status_code == 400
    assert '多个 SKILL.md' in multiple.json()['detail']


@pytest.mark.parametrize('version', [1, 2])
def test_native_import_uses_package_limit(app_env, version):
    client, store, service, repo = app_env
    if version == 1:
        content = pack_zip(catalog()[0]['id'])
    else:
        from factory.control.manifest_packs import export_pack
        content = export_pack(service.agent_manifests, service.agents, service.agents.list()[0]['id'])
    raw = io.BytesIO(content)
    with zipfile.ZipFile(raw, 'a') as z:
        for i in range(600): z.writestr(f'extra/{i}.txt', 'reference')
        z.writestr('__MACOSX/._trash','junk')
    response = client.post('/api/v4/agent-packs/import',headers=login(client),
        files={'file':('native.zip',raw.getvalue(),'application/zip')})
    assert response.status_code == 201, response.text


def test_mounted_directory_uses_package_count_and_filters_macos(tmp_path):
    root = tmp_path / 'skills'; root.mkdir()
    (root / 'SKILL.md').write_text('Root')
    for i in range(635): (root / f'{i}.txt').write_text('data')
    (root / '__MACOSX').mkdir()
    (root / '__MACOSX' / '._bad').symlink_to('/missing')
    assert len(inspect_skill(directory_zip(tmp_path,'skills'),max_files=3000)['files']) == 636
