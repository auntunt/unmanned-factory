"""Portable v2 job manifests with pinned skill bodies; read legacy v1 ZIPs."""
import io
import hashlib
import json
import uuid
import zipfile

from factory.control.agents import _validate_payload, inspect_skill, MAX_PACK_FILES, MAX_ZIP, strip_macos_junk, macos_junk
from factory.control.agent_manifests import encoded, compile_instructions
from factory.control.store import now, scrub


def export_pack(manifests, agents, aid):
    agent=agents.get(aid)
    manifest=manifests.get(aid)
    version=agents.version(aid)
    available = [entry['skill'] for entry in manifest.get('adaptation', {}).get('available_skills', [])]
    refs = list({ref['id']: ref for ref in [*manifest['skills'], *available]}.values())
    skills=manifests.resolve({**manifest, 'skills': refs})
    config={k:version[k] for k in ('model_settings','tool_scope','delivery')}
    with manifests.store.connect() as db:
        library_ids = [row[0] for row in db.execute("SELECT id FROM skill_assets WHERE agent_id=? AND (json_extract(data,'$.source')='skill-ingestion' OR json_extract(data,'$.library')=1)", (aid,))]
    external_ids = [s['external_source']['asset_id'] for s in skills
                    if s.get('external_source', {}).get('asset_id')]
    body={'schema':'webuddy.agent-pack/v2','name':agent['name'],'purpose':agent['purpose'],
          'manifest':manifest,'configuration':config,
          'skill_metadata':[{k:v for k,v in s.items() if k!='instructions'} for s in skills],
          'assets':list(dict.fromkeys([*version.get('skill_ids',[]), *library_ids, *external_ids]))}
    with manifests.store.connect() as db:
        body['asset_metadata'] = {sid: {key: value for key, value in json.loads(db.execute(
            'SELECT data FROM skill_assets WHERE id=? AND agent_id=?', (sid, aid)).fetchone()[0]).items()
            if key in ('sha256', 'source_sha256')} for sid in body['assets']}
    asset_bodies = {sid: agents.skill_body(sid, agent_id=aid) for sid in body['assets']}
    body['asset_chunks'] = {sid: [f'assets/{sid}/part-{i}' for i in range((len(raw) + 1048575) // 1048576)]
                            for sid, raw in asset_bodies.items() if len(raw) > 2 * 1048576}
    body['library_assets'] = [sid for sid in body['assets'] if sid not in version.get('skill_ids', [])]
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json',encoded(body))
        for s in skills:z.writestr(f"skills/{s['id']}@{s['version']}.md",s['instructions'])
        for sid, raw in asset_bodies.items():
            if sid in body['asset_chunks']:
                for index, path in enumerate(body['asset_chunks'][sid]):
                    z.writestr(path, raw[index * 1048576:(index + 1) * 1048576], compress_type=zipfile.ZIP_STORED)
            else:
                z.writestr(f'assets/{sid}.zip',raw, compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


def _asset_content(archive, files, pack, old_sid):
    path = f'assets/{old_sid}.zip'
    chunks = pack.get('asset_chunks', {}).get(old_sid)
    if chunks is not None:
        if (not isinstance(chunks, list) or not 1 <= len(chunks) <= 20
                or chunks != [f'assets/{old_sid}/part-{i}' for i in range(len(chunks))]
                or any(p not in files for p in chunks)
                or sum(archive.getinfo(p).file_size for p in chunks) > MAX_ZIP):
            raise ValueError('职能包附件分片无效或超过 20 MiB')
        return b''.join(archive.read(p) for p in chunks)
    if path not in files:
        raise ValueError('职能包缺少附件')
    return archive.read(path)


def import_pack(manifests, raw, actor):
    inspect_skill(raw, max_files=MAX_PACK_FILES)  # Bound size, reject traversal, symlinks and ambiguous names.
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        files={path for path in z.namelist() if not macos_junk(path)}
        v2='manifest.json' in files
        name='manifest.json' if v2 else 'agent.json'
        if name not in files:
            raise ValueError('此入口仅支持 webuddy 导出的职能包（缺少 manifest.json 或 agent.json）。外部 skill ZIP 请使用“导入外部 skill 包”，在职能体详情的“为职能体添加能力”开始适配；通过独立验收并人签后才会启用。')
        try:pack=json.loads(z.read(name))
        except (ValueError,UnicodeError):raise ValueError('职能包清单不是有效 JSON') from None
        if not isinstance(pack,dict) or pack.get('schema')!=('webuddy.agent-pack/v2' if v2 else 'webuddy.agent-pack/v1'):
            raise ValueError('不支持的职能包版本')
        if not isinstance(pack.get('name'),str) or not pack['name'].strip() or len(pack['name'])>120:raise ValueError('职能体名称无效')
        if not isinstance(pack.get('purpose',''),str) or len(pack.get('purpose',''))>4000:raise ValueError('职能体用途无效')
        config=_validate_payload(pack.get('configuration',{}))
        aid=uuid.uuid4().hex; at=now(); imported=[]
        with manifests.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if v2:
                m=pack.get('manifest')
                if not isinstance(m,dict):raise ValueError('职能包缺少 manifest')
                refs=m.get('skills')
                if not isinstance(refs,list) or len(refs)>24:raise ValueError('skill 清单无效')
                asset_ids = pack.get('assets', [])
                if not isinstance(asset_ids, list) or len(asset_ids) > 100 or any(not isinstance(x, str) for x in asset_ids) or len(set(asset_ids)) != len(asset_ids):
                    raise ValueError('附件清单无效')
                asset_remap = {sid: uuid.uuid4().hex for sid in asset_ids}
                metadata=pack.get('skill_metadata',[])
                if not isinstance(metadata,list):raise ValueError('skill 元数据无效')
                available = [entry['skill'] for entry in (m.get('adaptation') or {}).get('available_skills', [])]
                all_refs = list({ref['id']: ref for ref in [*refs, *available]}.values())
                if len(all_refs) > MAX_PACK_FILES:
                    raise ValueError('随附能力过多')
                for ref in all_refs:
                    if not isinstance(ref,dict) or set(ref)!={'id','version'} or not isinstance(ref['id'],str) or type(ref['version']) is not int or ref['version']<1:
                        raise ValueError('skill 版本引用无效')
                    path=f"skills/{ref['id']}@{ref['version']}.md"
                    if path not in files:raise ValueError('职能包缺少引用的 skill 正文')
                    text=z.read(path).decode('utf-8')
                    meta=next((s for s in metadata if isinstance(s,dict) and s.get('id')==ref['id'] and s.get('version')==ref['version']),{})
                    mid=uuid.uuid4().hex
                    skill={'id':mid,'version':1,'name':str(meta.get('name','导入能力'))[:120],'description':'从职能包导入',
                           'category':meta.get('category') if meta.get('category') in ('style','knowledge','workflow','delivery') else 'workflow',
                           'instructions':text,'status':'ready','source':{'type':'pack','id':ref['id'],'version':ref['version']},'actor':str(actor),'updated_at':at}
                    for field in ('requires_authorization', 'external_source'):
                        if field in meta:
                            skill[field] = meta[field]
                    if meta.get('owner_agent_id'):
                        skill['owner_agent_id'] = aid
                    if skill.get('external_source'):
                        skill['external_source'] = {**skill['external_source'], 'agent_id': aid}
                        old_asset = skill['external_source'].get('asset_id')
                        if old_asset is None:
                            # Earlier exports lost the local id; recover only by exact signed ZIP hash.
                            matches = [sid for sid in asset_ids if hashlib.sha256(
                                _asset_content(z, files, pack, sid)).hexdigest() == skill['external_source'].get('package_sha256')]
                            if len(matches) == 1:
                                old_asset = matches[0]
                        if old_asset not in asset_remap:
                            raise ValueError('外部 Skill 缺少来源附件')
                        skill['external_source']['asset_id'] = asset_remap[old_asset]
                    clean_skill = scrub(skill)
                    if skill.get('external_source'):
                        clean_skill['instructions'] = text
                    db.execute('INSERT INTO instruction_modules VALUES(?,?,?)',(mid,1,encoded(clean_skill)))
                    imported.append({'id':mid,'version':1})
                remapped = {ref['id']: new for ref,new in zip(all_refs,imported)}
                payload={'identity':m.get('identity'),'skills':[remapped[r['id']] for r in refs],'assertions':m.get('assertions')}
                if m.get('adaptation'):
                    if not isinstance(m['adaptation'], dict) or not isinstance(m['adaptation'].get('steps', []), list):
                        raise ValueError('职能包适配结构无效')
                    payload['adaptation'] = m['adaptation']
                    for step in [*payload['adaptation'].get('steps', []), *payload['adaptation'].get('available_skills', [])]:
                        if not isinstance(step, dict) or not isinstance(step.get('skill', {}), dict):
                            raise ValueError('SOP skill 引用无效')
                        old_ref = step.get('skill') or {}
                        if old_ref.get('id') in remapped:
                            step['skill'] = remapped[old_ref['id']]
                manifests._validate(payload,db)
                compiler='legacy-exact-v1' if m.get('compiler')=='legacy-exact-v1' else 'composition-v2'
                if compiler=='legacy-exact-v1' and len(imported)!=1:raise ValueError('遗留兼容清单必须引用一个完整 skill')
                compile_instructions({**payload,'compiler':compiler},manifests.resolve(payload,db))
                config['acceptance']=payload['assertions']
                assets=pack.get('assets',[])
                if not isinstance(assets,list) or len(assets)>100:raise ValueError('附件清单无效')
                config['skill_ids']=[]
                for old_sid in assets:
                    content = _asset_content(z, files, pack, old_sid)
                    inspect_skill(content, max_files=MAX_PACK_FILES)
                    imported_hash = hashlib.sha256(content).hexdigest()
                    content = strip_macos_junk(content)
                    meta=inspect_skill(content, max_files=MAX_PACK_FILES); sid=asset_remap[old_sid]
                    data={**meta,'id':sid,'agent_id':aid,'filename':'imported.zip','source':'pack','library':old_sid in pack.get('library_assets', []),'created_at':at}
                    data['imported_sha256'] = imported_hash
                    data['source_sha256'] = (pack.get('asset_metadata', {}).get(old_sid, {}).get('source_sha256')
                                             or imported_hash)
                    db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)',(sid,aid,encoded(data),content,at))
                    if old_sid not in pack.get('library_assets', []):
                        config['skill_ids'].append(sid)
            else:
                config['skill_ids']=[]  # v1 ids are instance-local; preserve its whole archive as an asset.
                sid=uuid.uuid4().hex; meta=inspect_skill(raw, max_files=MAX_PACK_FILES)
                data={**meta,'id':sid,'agent_id':aid,'filename':'legacy-pack.zip','source':'pack-v1','created_at':at}
                db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)',(sid,aid,encoded(data),strip_macos_junk(raw),at));config['skill_ids']=[sid]
            agent={'id':aid,'name':pack['name'],'purpose':pack.get('purpose',''),'active_version':1,'actor':str(actor),'created_at':at,'updated_at':at}
            version={'id':uuid.uuid4().hex,'agent_id':aid,'version':1,**config,'source':'pack','previous_version':None,'created_at':at}
            db.execute('INSERT INTO agents VALUES(?,?)',(aid,encoded(scrub(agent))))
            db.execute('INSERT INTO agent_versions VALUES(?,?,?,?,?)',(version['id'],aid,1,encoded(scrub(version)),at))
            if v2:
                value={**payload,'agent_id':aid,'revision':1,'agent_version':1,'compiler':compiler,'created_at':at}
                manifests._insert(db,aid,scrub(value),actor,'pack.imported')
            else:manifests._current(db,aid)
    return agent
