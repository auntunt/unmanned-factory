"""CSV 工程量清单 → 标准 XML 的转换入口。

契约：stdin 读一个 JSON 请求 {input_dir, output_dir, inputs, options}，stdout 写一个
JSON 结果 {status, outputs, result, diagnostics, error_code}。不联网、不读 input/ 与
output/ 以外的路径、不改自身程序文件。

金额一律用 Decimal 计算并按 ROUND_HALF_UP 保留两位；浮点数不参与金额。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quote_xml import ParseError, parse_quote_csv, render_quote_xml  # noqa: E402


def main() -> int:
    request = json.load(sys.stdin)
    out_dir = Path(request['output_dir'])
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = request.get('inputs') or []
    if len(inputs) != 1:
        json.dump({'status': 'error', 'error_code': 'bad_input_count',
                   'error': '本工具一次只处理一个 CSV 清单', 'outputs': []}, sys.stdout)
        return 0
    source = Path(request['input_dir']) / Path(inputs[0]['name']).name
    try:
        raw = source.read_bytes().decode('utf-8-sig')
    except (OSError, UnicodeDecodeError):
        json.dump({'status': 'error', 'error_code': 'not_utf8',
                   'error': '清单必须是 UTF-8 编码的 CSV', 'outputs': []}, sys.stdout)
        return 0
    try:
        quote = parse_quote_csv(raw, project=(request.get('options') or {}).get('project_name') or '未命名工程')
    except ParseError as exc:
        json.dump({'status': 'error', 'error_code': exc.code, 'error': str(exc),
                   'outputs': [], 'diagnostics': exc.diagnostics}, sys.stdout)
        return 0
    (out_dir / 'quote.xml').write_bytes(render_quote_xml(quote).encode('utf-8'))
    json.dump({'status': 'ok', 'outputs': [{'path': 'quote.xml', 'kind': 'document'}],
               'result': {'item_count': len(quote['items']), 'total': quote['total']},
               'diagnostics': quote['diagnostics']}, sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
