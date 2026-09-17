"""MFD → 江苏标准 XML（局部提取版）的调用入口。

契约：stdin 一个 JSON 请求，stdout 一个 JSON 结果。不联网、只读 input/、只写 output/。

**能力边界**：本工具做的是清单记录的局部提取，不是整文件无损转换。只要还有记录
没能解析出 XSD 必填字段，就以 `incomplete_extraction` 失败返回——候选 XML 仍然写出
并可下载，但绝不标记为已验证的转换结果。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mfd_lex import NotMfd, lex, stream_start  # noqa: E402
from mfd_records import records as extract_records  # noqa: E402
from xml_writer import render  # noqa: E402


def main() -> int:
    request = json.load(sys.stdin)
    out_dir = Path(request['output_dir'])
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = request.get('inputs') or []
    if len(inputs) != 1:
        json.dump({'status': 'error', 'error_code': 'bad_input_count',
                   'error': '本工具一次只处理一个 MFD 文件', 'outputs': []}, sys.stdout)
        return 0
    raw = (Path(request['input_dir']) / Path(inputs[0]['name']).name).read_bytes()
    try:
        start = stream_start(raw)
        tokens, skipped = lex(raw, start)
    except NotMfd as exc:
        json.dump({'status': 'error', 'error_code': 'not_mfd', 'error': str(exc), 'outputs': []}, sys.stdout)
        return 0
    found = extract_records(tokens)
    resolved = [record for record in found if not record['missing']]
    unresolved = [record for record in found if record['missing']]
    report = {
        'stream_start': start, 'file_bytes': len(raw), 'tokens': len(tokens),
        'bytes_not_interpreted': skipped,
        'note': ('局部词法扫描，未做整体分帧；bytes_not_interpreted 是未被任何已确认词素消费的字节数，'
                 '不能当作转换完整度。'),
        'records': [{'offset': f"0x{record['offset']:x}", '编号': record['code'], '名称': record['name'],
                     '单位': record['unit'], '数值': {k: repr(v) for k, v in record['values'].items()},
                     '未解析字段': record['missing']} for record in found],
    }
    (out_dir / 'extraction_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir / 'project.xml').write_bytes(render(resolved).encode('utf-8'))
    outputs = [{'path': 'project.xml', 'kind': 'document'}, {'path': 'extraction_report.json', 'kind': 'document'}]
    result = {'records_total': len(found), 'records_emitted': len(resolved),
              'records_unresolved': len(unresolved), 'complete': not unresolved}
    if not found:
        json.dump({'status': 'error', 'error_code': 'no_records', 'outputs': outputs, 'result': result,
                   'error': '没有识别到任何清单记录；该文件可能是本工具尚未覆盖的 MFD 变体'}, sys.stdout)
        return 0
    if unresolved:
        json.dump({'status': 'error', 'error_code': 'incomplete_extraction', 'outputs': outputs, 'result': result,
                   'error': f'{len(unresolved)}/{len(found)} 条清单记录缺少 XSD 必填字段（单位/工程量），'
                            f'已写出候选 XML 但不构成完整转换；缺失明细见 extraction_report.json',
                   'diagnostics': [f"0x{record['offset']:x} {record['code']} 缺 {'、'.join(record['missing'])}"
                                   for record in unresolved[:20]]}, sys.stdout)
        return 0
    json.dump({'status': 'ok', 'outputs': outputs, 'result': result,
               'diagnostics': [report['note']]}, sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
