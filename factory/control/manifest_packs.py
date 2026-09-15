"""Portable v2 job manifests with pinned skill bodies; read legacy v1 ZIPs."""
import io
import json
import uuid
import zipfile

from factory.control.agents import _validate_payload, inspect_skill, MAX_PACK_FILES, strip_macos_junk, macos_junk
from factory.control.agent_manifests import encoded, compile_instructions
from factory.control.store import now, scrub


def export_pack(manifests, agents, aid):
    agent=agents.get(aid)
    manifest=manifests.get(aid)
    version=agents.version(aid)
    skills=manifests.resolve(manifest)
    config={k:version[k] for k in ('model_settings','tool_scope','delivery')}
    body={'schema':'webuddy.agent-pack/v2','name':agent['name'],'purpose':agent['purpose'],
          'manifest':manifest,'configuration':config,
          'skill_metadata':[{k:v for k,v in s.items() if k!='instructions'} for s in skills],
          'assets':version.get('skill_ids',[])}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json',encoded(body))
        for s in skills:z.writestr(f"skills/{s['id']}@{s['version']}.md",s['instructions'])
        for sid in body['assets']:z.writestr(f'assets/{sid}.zip',agents.skill_body(sid,agent_id=aid))
    return out.getvalue()


def import_pack(manifests, raw, actor):
    inspect_skill(raw, max_files=MAX_PACK_FILES)  # Bound size, reject traversal, symlinks and ambiguous names.
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        files={path for path in z.namelist() if not macos_junk(path)}
        v2='manifest.json' in files
        name='manifest.json' if v2 else 'agent.json'
        if name not in files:
            raise ValueError('此入口仅支持 webuddy 导出的职能包（缺少 manifest.json 或 agent.json）。外部 skill ZIP 请使用“导入外部 skill 包”，选择所属项目后开始适配；通过独立验收并人签后才会启用。')
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
                metadata=pack.get('skill_metadata',[])
                if not isinstance(metadata,list):raise ValueError('skill 元数据无效')
                for ref in refs:
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
                    clean_skill = scrub(skill)
                    if skill.get('external_source'):
                        clean_skill['instructions'] = text
                    db.execute('INSERT INTO instruction_modules VALUES(?,?,?)',(mid,1,encoded(clean_skill)))
                    imported.append({'id':mid,'version':1})
                payload={'identity':m.get('identity'),'skills':imported,'assertions':m.get('assertions')}
                if m.get('adaptation'):
                    if not isinstance(m['adaptation'], dict) or not isinstance(m['adaptation'].get('steps', []), list):
                        raise ValueError('职能包适配结构无效')
                    payload['adaptation'] = m['adaptation']
                    remapped = {ref['id']: new for ref,new in zip(refs,imported)}
                    for step in payload['adaptation'].get('steps', []):
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
                    path=f'assets/{old_sid}.zip'
                    if path not in files:raise ValueError('职能包缺少附件')
                    content=z.read(path); meta=inspect_skill(content, max_files=MAX_PACK_FILES); sid=uuid.uuid4().hex
                    content=strip_macos_junk(content)
                    data={**meta,'id':sid,'agent_id':aid,'filename':'imported.zip','source':'pack','created_at':at}
                    db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)',(sid,aid,encoded(data),content,at));config['skill_ids'].append(sid)
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
