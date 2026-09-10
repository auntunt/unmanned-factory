"""Build reproducible first-party ZIP distributions, without model calls."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import zipfile
from factory.control.agent_packs import ROOT, catalog, pack_zip
from factory.control.agents import inspect_skill


def archive(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 9, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, body)
    return out.getvalue()


def export(destination):
    destination.mkdir(parents=True, exist_ok=True)
    files = {p['id'] + '.zip': pack_zip(p['id']) for p in catalog()}
    modules = [json.loads(p.read_text()) for p in sorted((ROOT / 'modules').glob('*.json'))]
    skill = ('---\nname: webuddy-core-modules\ndescription: "可复用的项目理解、渐进修改、CLI 交付和能力迭代方法"\n---\n\n'
        '# 共用能力模块\n\n按任务选择适用的章节，不必让每个任务走遍所有步骤。\n\n' +
        '\n\n'.join(f"## {m['name']}\n适用：{m['description']}\n\n{m['instructions']}" for m in modules))
    module_files = {f"modules/{m['id']}.json": json.dumps(m, ensure_ascii=False, indent=2).encode() for m in modules}
    module_files['SKILL.md'] = skill.encode()
    files['shared-modules.zip'] = archive(module_files)
    inspect_skill(files['shared-modules.zip'])
    files['README.md'] = ('# webuddy 内置职能包\n\n四个独立职能包和共用模块 ZIP。整体合集请先解压，'
        '再选择一个职能包上传至已有职能体的维护对话；不要把合集当作单一 Skill。\n'
        '控制台已经提供内置职能体，可直接关联项目开始。下载的是平台原始模板，不包含团队后续维护变更。\n'
        'agent.json 保存配置；不自动执行脚本或赋予权限。评测样例待实际执行，不代表业务完成率。\n').encode()
    files['SHA256SUMS'] = ''.join(f'{hashlib.sha256(body).hexdigest()}  {name}\n' for name, body in sorted(files.items())).encode()
    for name, body in files.items():
        (destination / name).write_bytes(body)
    (destination / 'webuddy-starter-packs.zip').write_bytes(archive(files))
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('output/agent-packs'))
    print(export(parser.parse_args().out))
