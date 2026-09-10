"""First-party, reproducible starter packs; installs never overwrite team revisions."""
from __future__ import annotations

import io
import json
from pathlib import Path
import uuid
import zipfile

from factory.control.agents import AgentStore, _validate_payload, inspect_skill
from factory.control.modules import ModuleStore
from factory.control.store import now

ROOT = Path(__file__).with_name('builtin_packs')


def catalog():
    return [json.loads(p.read_text()) for p in sorted((ROOT / 'packs').glob('*/pack.json'))]


def pack_source(slug):
    # Resolve against the known catalog, never a request-derived filesystem path.
    pack = next((p for p in catalog() if p['id'] == slug), None)
    if pack is None:
        raise KeyError(slug)
    modules = [json.loads((ROOT / 'modules' / f'{mid}.json').read_text()) for mid in pack['modules']]
    return pack, modules


def configuration(pack, modules):
    instructions = pack['instructions'] + '\n\n' + '\n\n'.join(
        f"## {m['name']} · v{m['version']}\n{m['instructions']}" for m in modules)
    return _validate_payload(dict(instructions=instructions, model_settings=pack['model_settings'],
        tool_scope=pack['tool_scope'], acceptance=pack['acceptance'],
        delivery={'description': pack['purpose'], 'format': 'project', 'artifacts': pack['artifacts']}))


def pack_zip(slug):
    pack, modules = pack_source(slug)
    config = configuration(pack, modules)
    skill = (f"---\nname: {slug}\ndescription: {json.dumps(pack['purpose'], ensure_ascii=False)}\n---\n\n"
        f"# {pack['name']}\n\n{config['instructions']}\n\n## 输入\n" +
        '\n'.join('- ' + x for x in pack['inputs']) + '\n\n## 验收\n' +
        '\n'.join('- ' + x for x in pack['acceptance']) +
        '\n\n## 样例\n参考 evaluations.json 与 fixtures/。样例是验收任务，不代表职能体已经通过业务评测。\n')
    manifest = {'schema': 'webuddy.agent-pack/v1', 'id': slug, 'version': pack['version'],
        'name': pack['name'], 'purpose': pack['purpose'], 'configuration': config,
        'module_sources': [{'id': m['id'], 'version': m['version']} for m in modules],
        'validation_status': pack['validation_status']}
    files = {'SKILL.md': skill, 'agent.json': json.dumps(manifest, ensure_ascii=False, indent=2),
        'evaluations.json': json.dumps(pack['examples'], ensure_ascii=False, indent=2),
        'README.md': f"# {pack['name']}\n\n{pack['purpose']}\n\n此包为 webuddy 原创入门模板。无密钥、无自动执行安装脚本。\n"
            '控制台已内置对应职能体，可直接关联项目使用。也可在已有职能体的维护模式上传本 ZIP，'
            '要求读取 SKILL.md 整理草稿，再按平台流程应用。上传 Skill 不会自动创建职能体或自动执行脚本。\n\n'
            'agent.json 是可移植配置说明；当前没有通用职能包自动导入接口。modules/ 保留模块来源版本，'
            '配置中的指导是构建时快照，平台尚未提供动态模块引用。模型与工具继承平台设置。\n\n'
            'fixtures/ 仅用于隔离验收，不是生产程序。先完成 evaluations.json 中的任务，再判断是否适合团队业务。\n'}
    for m in modules:
        files[f"modules/{m['id']}.json"] = json.dumps(m, ensure_ascii=False, indent=2)
    for path in sorted((ROOT / 'packs' / slug / 'fixtures').rglob('*')):
        if path.is_file():
            files['fixtures/' + path.relative_to(ROOT / 'packs' / slug / 'fixtures').as_posix()] = path.read_text()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, content.encode('utf-8'))
    raw = output.getvalue()
    inspect_skill(raw)
    return raw


def install_builtins(store):
    """Atomic initial install, deterministic identity, no update of existing agents."""
    AgentStore(store)
    ModuleStore(store)
    prepared = []
    for pack in catalog():
        p, modules = pack_source(pack['id'])
        prepared.append((p, modules, configuration(p, modules), pack_zip(p['id'])))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for pack, modules, config, raw in prepared:
            aid = uuid.uuid5(uuid.NAMESPACE_URL, 'webuddy:agent-pack:' + pack['id']).hex
            if db.execute('SELECT 1 FROM agents WHERE id=?', (aid,)).fetchone():
                continue
            at = now()
            sid = uuid.uuid5(uuid.NAMESPACE_URL, 'webuddy:agent-pack-skill:' + pack['id']).hex
            for m in modules:
                data = {**m, 'actor': 'platform', 'updated_at': at}
                db.execute('INSERT OR IGNORE INTO instruction_modules VALUES(?,?,?)',
                    (m['id'], m['version'], json.dumps(data, ensure_ascii=False)))
            config['skill_ids'] = [sid]
            version = {'id': uuid.uuid4().hex, 'agent_id': aid, 'version': 1, **config,
                'source': 'builtin-pack', 'previous_version': None, 'created_at': at}
            agent = {'id': aid, 'name': pack['name'], 'purpose': pack['purpose'],
                'active_version': 1, 'created_at': at, 'updated_at': at, 'actor': 'platform',
                'builtin_pack': pack['id'], 'builtin_pack_version': pack['version']}
            skill = {**inspect_skill(raw), 'id': sid, 'agent_id': aid, 'filename': pack['id'] + '.zip',
                'source': 'builtin-pack', 'created_at': at}
            db.execute('INSERT INTO agents VALUES(?,?)', (aid, json.dumps(agent, ensure_ascii=False)))
            db.execute('INSERT INTO agent_versions VALUES(?,?,?,?,?)',
                (version['id'], aid, 1, json.dumps(version, ensure_ascii=False), at))
            db.execute('INSERT INTO skill_assets VALUES(?,?,?,?,?)',
                (sid, aid, json.dumps(skill, ensure_ascii=False), raw, at))
