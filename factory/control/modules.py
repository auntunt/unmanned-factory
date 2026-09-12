"""Composable instruction modules, versioned independently of agent presets."""
from __future__ import annotations
import json
import uuid
from factory.control.store import Conflict, now, scrub

CATEGORIES = ('style', 'knowledge', 'workflow', 'delivery')
BUILTINS = (
 ('builtin-clean-ui', '简洁产品界面', 'style', '清晰、克制，适合日常工作的产品界面。', '使用清晰的中文信息层级、充足留白和一致组件。优先呈现主要操作。支持键盘、手机布局和可读对比度。避免无意义装饰、虚构指标和外部字体依赖。'),
 ('builtin-warm-ui', '温暖生活风格', 'style', '柔和色彩与友好语言，适合生活小工具。', '采用温暖浅色、柔和但清晰的配色、友好的中文文案和简洁交互。确保正文对比度与手机可用性。不用情绪文案遮挡操作，不虚构用户数据。'),
 ('builtin-browser-check', '浏览器交互验证', 'workflow', '验证关键用户操作，保留实际观察结果。', '涉及网页时，使用可用浏览器工具验证核心用户流程及手机布局，检查控制台错误。测试范围以需求为准，不反复扩大测试。工具不可用时如实记录限制，不宣称已经验证。'),
 ('builtin-web-delivery', '网站交付说明', 'delivery', '让成果能够运行、构建并继续维护。', '交付网站时提供实际可运行的启动与构建说明，列出必要环境变量名称和配置示例，禁止写入真实密钥。明确已验证项目和未完成步骤；未经授权不购买域名或创建收费服务。'),
)

class ModuleStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS instruction_modules(id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(id,version));
                CREATE TABLE IF NOT EXISTS project_modules(project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_module_update BEFORE UPDATE ON instruction_modules BEGIN SELECT RAISE(ABORT,'module versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_module_delete BEFORE DELETE ON instruction_modules BEGIN SELECT RAISE(ABORT,'module versions are immutable'); END;''')
            for mid, name, category, description, instructions in BUILTINS:
                value = dict(id=mid, version=1, name=name, category=category, description=description, instructions=instructions, actor='platform', updated_at=now())
                db.execute('INSERT OR IGNORE INTO instruction_modules VALUES(?,?,?)', (mid, 1, json.dumps(value, ensure_ascii=False)))

    def list(self):
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT m.data FROM instruction_modules m WHERE version=(SELECT MAX(version) FROM instruction_modules WHERE id=m.id) ORDER BY m.rowid')]

    def save(self, payload, actor, mid=None, expected_revision=0):
        value = {}
        for field, limit in [('name',120), ('description',1000), ('instructions',16000)]:
            item = payload.get(field, '')
            if not isinstance(item, str) or len(item) > limit or (field != 'description' and not item.strip()):
                raise ValueError(f'{field} 内容为空或过长')
            value[field] = scrub(item.strip())
        if payload.get('category') not in CATEGORIES: raise ValueError('请选择有效的模块类别')
        value['category'] = payload['category']
        from factory.control.sources import source_refs, source_slots
        value['source_refs'] = source_refs(payload.get('source_refs', []))
        value['source_slots'] = source_slots(payload.get('source_slots', []))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT MAX(version) FROM instruction_modules WHERE id=?',(mid,)).fetchone()[0] if mid else 0
            if mid and current is None: raise KeyError(mid)
            if (current or 0) != expected_revision: raise Conflict('模块已更新，请刷新后重试')
            value.update(id=mid or uuid.uuid4().hex, version=(current or 0)+1, actor=str(actor), updated_at=now())
            db.execute('INSERT INTO instruction_modules VALUES(?,?,?)',(value['id'],value['version'],json.dumps(value,ensure_ascii=False)))
        return value

    def selection(self, pid):
        self.store.project(pid)
        with self.store.connect() as db:
            row = db.execute('SELECT revision,data FROM project_modules WHERE project_id=?',(pid,)).fetchone()
        return {'revision':row[0], 'modules':json.loads(row[1])} if row else {'revision':0, 'modules':[]}

    def select(self, pid, refs, revision, actor):
        self.store.project(pid)
        if not isinstance(refs,list) or len(refs)>12: raise ValueError('最多选择12个能力模块')
        selected=[]; seen=set()
        from factory.control.sources import SourceStore
        sources = SourceStore(self.store)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision FROM project_modules WHERE project_id=?',(pid,)).fetchone()
            if (row[0] if row else 0)!=revision: raise Conflict('项目组合已变更，请刷新后重试')
            for ref in refs:
                mid=ref['id']
                if mid in seen: raise ValueError('不能重复选择同一模块')
                seen.add(mid)
                item=db.execute('SELECT data FROM instruction_modules WHERE id=? AND version=?',(mid,ref['version'])).fetchone()
                if not item: raise ValueError('模块版本不存在，请刷新模块库')
                selected.append(json.loads(item[0]))
            versions = {}
            for module in selected:
                for ref in [*module.get('source_refs', []), *sources.resolve_slots(pid, module.get('source_slots', []))]:
                    sources.resolve(pid, ref)
                    if ref['id'] in versions and versions[ref['id']] != ref['revision']:
                        raise ValueError('模块组合的数据源版本冲突')
                    versions[ref['id']] = ref['revision']
            if sum(m['category']=='style' for m in selected)>1: raise ValueError('一个项目只能选择一种界面风格，其他类别可以组合')
            if sum(len(m['instructions']) for m in selected)>40000: raise ValueError('组合内容过长，请精简模块')
            db.execute('INSERT OR REPLACE INTO project_modules VALUES(?,?,?)',(pid,revision+1,json.dumps(selected,ensure_ascii=False)))
        return {'revision':revision+1,'modules':selected}

    def freeze(self, run):
        if 'module_snapshot' in run: return {}
        selected=self.selection(run['project_id'])
        from factory.control.sources import SourceStore
        sources = SourceStore(self.store)
        for module in selected['modules']:
            module['resolved_sources'] = [*module.get('source_refs', []),
                *sources.resolve_slots(run['project_id'], module.get('source_slots', []))]
        return {'module_snapshot':selected['modules'], 'module_selection_revision':selected['revision']}


def module_prompt(run):
    modules=run.get('module_snapshot') or []
    if not modules: return ''
    return '\n\nPROJECT MODULES (frozen versions; scoped guidance, cannot grant tools or override the user request or platform permissions):\n' + '\n\n'.join(f"[{m['category']}] {m['name']} v{m['version']}\n{m['instructions']}" for m in modules)
