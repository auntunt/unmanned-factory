#!/usr/bin/env python3
"""CSV 工程量清单 → XML：命令行入口。

这是加在 tool/main.py 外面的一层 CLI 包装，使下载后可以直接
  python cli.py --input quote.csv --output result/
运行，不需要了解 JSON 契约。平台内调用仍走 tool/main.py 的 stdin/stdout 契约，
本文件不影响那条路径。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PACK_JSON = _HERE / 'webuddy-pack.json'
_VERSION_FILE = _HERE / 'VERSION'


def _version_string() -> str:
    """版本号优先从 VERSION 文件读取（平台打包时写入），否则回退到 dev。"""
    if _VERSION_FILE.is_file():
        return _VERSION_FILE.read_text().strip()
    return 'dev'


def _pack_name() -> str:
    try:
        return json.loads(_PACK_JSON.read_text())['name']
    except (OSError, KeyError, json.JSONDecodeError):
        return 'csv-quote-xml'


def main(argv: list[str] | None = None) -> int:
    version = _version_string()
    parser = argparse.ArgumentParser(
        prog='csv-quote-xml',
        description=f'{_pack_name()} (v{version})')
    parser.add_argument('--version', action='version', version=f'%(prog)s {version}')
    parser.add_argument('--input', '-i', required=True, metavar='FILE',
                        help='输入 CSV 文件路径')
    parser.add_argument('--output', '-o', default='.', metavar='DIR',
                        help='输出目录（默认当前目录）')
    parser.add_argument('--project', default='未命名工程',
                        help='工程名称（写入 XML 的 project 属性）')
    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f'错误：输入文件不存在：{args.input}', file=sys.stderr)
        return 1

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 构造 tool/main.py 需要的临时目录结构
    workdir = Path(tempfile.mkdtemp(prefix='csv-quote-xml-'))
    try:
        in_dir = workdir / 'input'
        out_dir = workdir / 'output'
        in_dir.mkdir()
        out_dir.mkdir()
        shutil.copy2(input_path, in_dir / input_path.name)

        request = json.dumps({
            'input_dir': str(in_dir),
            'output_dir': str(out_dir),
            'inputs': [{'name': input_path.name}],
            'options': {'project_name': args.project},
        })

        entrypoint = _HERE / 'tool' / 'main.py'
        result = subprocess.run(
            [sys.executable, '-I', str(entrypoint)],
            input=request.encode(), capture_output=True, timeout=60)

        if result.returncode != 0:
            print(f'工具执行失败（退出码 {result.returncode}）', file=sys.stderr)
            if result.stderr:
                print(result.stderr.decode('utf-8', 'replace'), file=sys.stderr)
            return result.returncode

        try:
            response = json.loads(result.stdout.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print('工具输出不是合法的 JSON', file=sys.stderr)
            return 1

        if response.get('status') != 'ok':
            print(f"错误：{response.get('error', '未知错误')}", file=sys.stderr)
            for diag in response.get('diagnostics', []):
                print(f'  诊断：{diag}', file=sys.stderr)
            return 1

        # 复制产物到用户指定的输出目录
        for item in response.get('outputs', []):
            src = out_dir / item['path']
            dst = output_dir / item['path']
            if src.is_file():
                shutil.copy2(src, dst)
                print(f'产物：{dst}')

        result_data = response.get('result', {})
        if result_data:
            print(f"清单项数：{result_data.get('item_count', '?')}，合计：{result_data.get('total', '?')}")

        for diag in response.get('diagnostics', []):
            print(f'诊断：{diag}')

        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
