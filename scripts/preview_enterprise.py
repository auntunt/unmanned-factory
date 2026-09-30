"""Strict synthetic modern-homepage rehearsal; never a production provider.

Only FIXED_GOAL is supported. Normal application schemas, specification policy,
managed workspace checks, review snapshots and acceptance ledgers remain active.
The model is scripted; functional evidence comes from the product check runner.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory.control.execution import _run_check
from factory.control.providers import ProviderError, ProviderResult
from factory.control.requirement_analysis import IDENTITY
from scripts.preview_v3 import RehearsalRunner, rehearsal_app

FIXED_GOAL = '创建一个命令行工具，运行 python hello.py 输出：工单服务已就绪'
EXPECTED_STDOUT = '工单服务已就绪\n'
SOURCE = 'print("工单服务已就绪")\n'
SPEC = {
    'goal': FIXED_GOAL,
    'screens': [],
    'flows': ['运行 python hello.py，标准输出为工单服务已就绪，退出码为 0。'],
    'data_model': ['输出为固定文本，不保存或读取用户数据。'],
    'non_goals': ['不访问网络，不部署，不修改其他项目。'],
    'risks_assumptions': ['明确标识的合成模型演练，仅支持这一个固定命令行需求。'],
}
SUPPORTED_CRITERIA = {
    '完成用户要求，并用实际运行或功能检查证明结果可用', FIXED_GOAL,
    '关键流程: ' + SPEC['flows'][0], '数据模型: ' + SPEC['data_model'][0],
    '非目标边界: ' + SPEC['non_goals'][0],
}


def _json_after(prompt, marker):
    if prompt.count(marker) != 1:
        raise ProviderError('合成演练缺少唯一明确的输入契约', transient=False)
    try:
        value, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1].lstrip())
        return value
    except (ValueError, TypeError):
        raise ProviderError('合成演练输入契约不是有效 JSON', transient=False) from None


def _signed_spec(root):
    paths = list((root / '.spec' / 'requirements').glob('*/spec.md'))
    if len(paths) != 1:
        raise ProviderError('合成演练要求唯一已确认的固定需求规格', transient=False)
    path = paths[0]
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(root)):
        raise ProviderError('合成演练规格路径不可为符号链接', transient=False)
    text = path.read_text(encoding='utf-8')
    value = _json_after(text, '## raw source\n\n')
    if text.count('## expanded spec') != 1:
        raise ProviderError('固定演练规格缺少唯一 expanded spec 区域', transient=False)
    if (not isinstance(value, dict) or '\nstatus: active\n' not in text or value.get('original_request') != FIXED_GOAL
            or value.get('spec_draft') != SPEC or value.get('fidelity_target') is not None):
        raise ProviderError('合成演练不支持未签署或已变化的规格', transient=False)
    return path


def _result(payload):
    return ProviderResult(json.dumps(payload, ensure_ascii=False), cost_usd=0.0,
                          tokens_in=0, tokens_out=0)


class EnterpriseRehearsalRunner(RehearsalRunner):
    """Keep the old seeded rehearsal, add only one bounded modern contract."""
    def run(self, request, emit, cancel=None):
        if cancel and cancel.is_set():
            raise ProviderError('合成演练已取消', transient=False)
        if request.prompt.startswith(IDENTITY):
            goal = _json_after(request.prompt, 'USER REQUEST (data):\n')
            if goal != FIXED_GOAL:
                raise ProviderError('此合成演练仅支持预先定义的命令行目标，未调用真实模型', transient=False)
            emit('assistant.message', {'text': '合成需求分析：只为固定 CLI 演练输出规格，不代表真实模型能力。'})
            return _result({'spec_draft': SPEC, 'recommended_skills': [], 'fidelity_target': None})
        root = Path(request.workspace).resolve()
        modern = (request.verification or (root / '.spec' / 'requirements').is_dir()
                  or request.prompt.startswith('You are the coding owner for this project.'))
        if not modern:
            # Only the two existing seeded legacy scenarios may use the old
            # rehearsal. An unrelated request must not produce welcome.txt.
            readme = root / 'README.md'
            marker = '这是用于工厂界面验收的隔离仓库。'
            legacy_site = (readme.is_file() and not readme.is_symlink()
                           and marker in readme.read_text(encoding='utf-8'))
            if request.read_only:
                goal = request.prompt.split('Request: ', 1)[-1].split('Prior planning history:', 1)[0].strip()
                supported = goal in ('请完善工单服务的欢迎提示，并验证交付结果。',
                                     '模糊需求：希望工单欢迎提示更合适。')
            else:
                supported = request.prompt.startswith('将 welcome.txt 更新为：工单服务已就绪。')
            if not legacy_site or not supported:
                raise ProviderError('合成演练不支持该请求；未调用真实模型', transient=False)
            return super().run(request, emit, cancel)
        spec_path = _signed_spec(root)
        if request.verification:
            return self._verify(request, root, emit, cancel)
        if request.read_only:
            # A non-continuous policy must still use registered checks, not the
            # legacy welcome check. The current homepage uses continuous mode.
            project = _json_after(request.prompt, 'Project: ')
            if set(project.get('checks', {})) != {'workspace-integrity'}:
                raise ProviderError('合成演练只支持既有 managed workspace 检查', transient=False)
            return _result({'title': '固定 CLI 合成演练', 'summary': FIXED_GOAL,
                'questions': [], 'tasks': [{'id': 'hello', 'title': FIXED_GOAL,
                'prompt': FIXED_GOAL, 'acceptance': [FIXED_GOAL], 'paths': ['hello.py'],
                'checks': ['workspace-integrity'], 'depends_on': [],
                'complexity': 'small', 'risk': 'low'}]})
        target = root / 'hello.py'
        if target.is_symlink():
            raise ProviderError('合成演练拒绝符号链接产物', transient=False)
        if target.exists() and target.read_text(encoding='utf-8') != SOURCE:
            raise ProviderError('合成演练拒绝覆盖未知源码', transient=False)
        node = spec_path.relative_to(root).as_posix()
        # A normal assistant declaration is observed by the production wrapper;
        # it creates the receipt after checking no edit happened yet. Never emit
        # a provider-forged scope_declaration receipt directly.
        emit('assistant.message', {'text': json.dumps({'scope_declaration': {'files': [
            {'path': 'hello.py', 'spec_nodes': [node]},
            {'path': node, 'spec_nodes': [node]},
        ]}}, ensure_ascii=False)})
        target.write_text(SOURCE, encoding='utf-8')
        spec_text = spec_path.read_text(encoding='utf-8')
        spec_text = spec_text.replace('\ncode:\nrelated:', '\ncode:\n- hello.py\nrelated:', 1)
        before_expanded, marker, _ = spec_text.partition('## expanded spec')
        if not marker:
            raise ProviderError('固定演练规格缺少 expanded spec 区域', transient=False)
        spec_path.write_text(before_expanded + marker + '\n\nhello.py 是固定 print 程序；'
            '独立验收使用产品 check runner 实际运行 Python 并核对标准输出和退出码。\n', encoding='utf-8')
        emit('assistant.message', {'text': '合成编码器写入固定 hello.py；平台检查和独立验收将实际运行并核对成果。'})
        return ProviderResult('固定 CLI 源码已生成（合成模型）。', cost_usd=0.0, tokens_in=0, tokens_out=0)

    def _verify(self, request, root, emit, cancel):
        criteria = _json_after(request.prompt, 'CRITERIA JSON:\n')
        if (not isinstance(criteria, list) or not criteria
                or any(not isinstance(row, dict) or row.get('text') not in SUPPORTED_CRITERIA
                       or not isinstance(row.get('id'), str) for row in criteria)):
            raise ProviderError('合成验收不支持该验收条件，不能生成通过结论', transient=False)
        target = root / 'hello.py'
        safe_source = (not target.is_symlink() and target.is_file()
                       and target.read_text(encoding='utf-8') == SOURCE)
        if not safe_source:
            evidence = '独立快照中的 hello.py 缺失或源码不等于固定演练程序；未执行未知源码。'
            return _result({'verdict': 'fail', 'reason': evidence,
                'criteria': [{'id': row['id'], 'status': 'fail', 'evidence': evidence} for row in criteria]})
        # This is the actual product command-check path, not a forged tool
        # receipt or an assertion that a host subprocess was an isolated tool.
        check = _run_check(root, 'synthetic-cli-functional-check',
            [sys.executable, '-I', 'hello.py'], min(15, request.timeout_s),
            lambda kind, payload, _task=None: emit(kind, payload), 'verification', cancel)
        passed = (check.get('exit') == 0 and not check.get('timeout')
                  and not check.get('cancelled') and check.get('stdout') == EXPECTED_STDOUT
                  and check.get('stderr') == '')
        evidence = ('合成独立验收，使用产品 check runner；源码逐字节匹配固定 print 程序；'
                    + json.dumps({k: check.get(k) for k in ('argv', 'exit', 'stdout', 'stderr', 'timeout', 'cancelled')}, ensure_ascii=False))
        return _result({'verdict': 'pass' if passed else 'fail', 'reason': evidence[:1500],
            'criteria': [{'id': row['id'], 'status': 'pass' if passed else 'fail',
                          'evidence': evidence} for row in criteria]})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    import uvicorn
    print('SYNTHETIC MODERN ENTRY: ' + FIXED_GOAL, flush=True)
    uvicorn.run(rehearsal_app(args.port, runner=EnterpriseRehearsalRunner()),
                host='127.0.0.1', port=args.port, proxy_headers=False)
