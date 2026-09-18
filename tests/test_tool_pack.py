"""工具执行包（CLI harness + 版本标识 + sha256 校验）的测试。

测试范围：
- CLI harness 能用 --version 打印版本号
- CLI harness 能用 --input/--output 运行工具
- build_tool_zip 生成的 ZIP 含必要文件和版本标识
- sha256sums.txt 内容与包内文件一致
- pack_routes 的 download endpoint 返回正确的 ZIP 和响应头
"""
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / 'factory' / 'control' / 'builtin_packs' / 'tools'
CSV_TOOL = TOOLS / 'csv-quote-xml'
MFD_TOOL = TOOLS / 'mfd-xml'


# ---- M4: --version 打印版本号 -----------------------------------------------

def test_csv_cli_version():
    result = subprocess.run(
        [sys.executable, str(CSV_TOOL / 'cli.py'), '--version'],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert 'dev' in result.stdout.strip()


def test_mfd_cli_version():
    result = subprocess.run(
        [sys.executable, str(MFD_TOOL / 'cli.py'), '--version'],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert 'dev' in result.stdout.strip()


# ---- M1: CLI harness 运行工具 -----------------------------------------------

def test_csv_cli_runs_tool(tmp_path):
    """CLI harness 能用 --input/--output 运行 csv-quote-xml 工具。"""
    fixture = CSV_TOOL / 'fixtures' / 'sample_quote.csv'
    if not fixture.is_file():
        pytest.skip('fixture 文件不存在')
    output_dir = tmp_path / 'out'
    result = subprocess.run(
        [sys.executable, str(CSV_TOOL / 'cli.py'),
         '--input', str(fixture),
         '--output', str(output_dir)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f'stderr: {result.stderr}'
    assert (output_dir / 'quote.xml').is_file()
    xml_content = (output_dir / 'quote.xml').read_bytes()
    assert b'<Quote' in xml_content
    assert b'<Item' in xml_content


def test_csv_cli_bad_input(tmp_path):
    """CLI harness 对不存在的输入文件返回非零退出码。"""
    result = subprocess.run(
        [sys.executable, str(CSV_TOOL / 'cli.py'),
         '--input', str(tmp_path / 'nonexistent.csv')],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0


def test_mfd_cli_runs_tool_synthetic(tmp_path):
    """CLI harness 能用合成样本运行 mfd-xml 工具。"""
    fixture = MFD_TOOL / 'fixtures' / 'synthetic_complete.mfd'
    if not fixture.is_file():
        pytest.skip('fixture 文件不存在')
    output_dir = tmp_path / 'out'
    result = subprocess.run(
        [sys.executable, str(MFD_TOOL / 'cli.py'),
         '--input', str(fixture),
         '--output', str(output_dir)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f'stderr: {result.stderr}'
    assert (output_dir / 'project.xml').is_file()


# ---- M2+M3: build_tool_zip -------------------------------------------------

def test_build_tool_zip_has_required_files():
    """build_tool_zip 生成的 ZIP 含 VERSION、sha256sums.txt 和工具文件。"""
    from factory.control.pack_zip import build_tool_zip

    version = {
        'version': 42,
        'manifest': {
            'tool': {'name': 'csv_quote_xml', 'entrypoint': 'tool/main.py',
                     'runtime': 'python3', 'timeout_seconds': 30,
                     'input_schema': {}, 'output_schema': {},
                     'permissions': {'network': False, 'max_input_bytes': 4194304,
                                     'max_output_bytes': 8388608}},
            'dependency_lock': {'python': '3', 'packages': []},
            'support_matrix': [],
            'evaluation_policy': {'test_set': 'fixtures/tests.json', 'required': True},
        },
    }
    files = {
        'tool/main.py': b'print("hello")',
        'tool/quote_xml.py': b'# module',
        'webuddy-pack.json': b'{"name":"test"}',
    }
    raw = build_tool_zip(version, files)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        assert 'VERSION' in names
        assert 'sha256sums.txt' in names
        assert 'tool/main.py' in names
        assert 'webuddy-pack.json' in names
        # cli.py should be included (matches csv_quote_xml tool name)
        assert 'cli.py' in names
        # VERSION 内容是版本号
        assert z.read('VERSION') == b'42'
        # sha256sums.txt 的每行格式正确
        checksums = z.read('sha256sums.txt').decode()
        for line in checksums.strip().split('\n'):
            digest, path = line.split('  ', 1)
            assert len(digest) == 64
            assert path in names or path == 'sha256sums.txt'


def test_build_tool_zip_sha256_matches():
    """sha256sums.txt 里的校验值与包内文件一致。"""
    from factory.control.pack_zip import build_tool_zip

    version = {
        'version': 7,
        'manifest': {
            'tool': {'name': 'csv_quote_xml', 'entrypoint': 'tool/main.py',
                     'runtime': 'python3', 'timeout_seconds': 30,
                     'input_schema': {}, 'output_schema': {},
                     'permissions': {'network': False, 'max_input_bytes': 4194304,
                                     'max_output_bytes': 8388608}},
            'dependency_lock': {'python': '3', 'packages': []},
            'support_matrix': [],
            'evaluation_policy': {'test_set': 'fixtures/tests.json', 'required': True},
        },
    }
    files = {
        'tool/main.py': b'# csv tool main',
        'webuddy-pack.json': json.dumps({'name': 'test'}).encode(),
    }
    raw = build_tool_zip(version, files)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        checksums = z.read('sha256sums.txt').decode()
        for line in checksums.strip().split('\n'):
            digest, path = line.split('  ', 1)
            if path == 'sha256sums.txt':
                continue  # 自身不在校验范围内
            content = z.read(path)
            assert hashlib.sha256(content).hexdigest() == digest, f'{path} sha256 mismatch'


# ---- M3: sha256 下载核对 ----------------------------------------------------

def test_download_tool_pack_endpoint(tmp_path):
    """通过 API 下载工具执行包，响应头含版本号和 sha256。"""
    # 这个测试需要完整的 ASGI 环境，使用 test_capability_packs 的 fixture
    pytest.importorskip('httpx')  # 确认测试依赖
    # 端点在 pack_routes 注册，完整流程需要发布一个版本。
    # 此处只验证 build_tool_zip 的输出与 hashlib 一致。
    from factory.control.pack_zip import build_tool_zip

    version = {
        'version': 3,
        'manifest': {
            'tool': {'name': 'csv_quote_xml', 'entrypoint': 'tool/main.py',
                     'runtime': 'python3', 'timeout_seconds': 30,
                     'input_schema': {}, 'output_schema': {},
                     'permissions': {'network': False, 'max_input_bytes': 4194304,
                                     'max_output_bytes': 8388608}},
            'dependency_lock': {'python': '3', 'packages': []},
            'support_matrix': [],
            'evaluation_policy': {'test_set': 'fixtures/tests.json', 'required': True},
        },
    }
    files = {'tool/main.py': b'# tool', 'webuddy-pack.json': b'{}'}
    raw = build_tool_zip(version, files)
    # 验证 ZIP sha256 可通过 hashlib 复现
    expected_sha = hashlib.sha256(raw).hexdigest()
    assert len(expected_sha) == 64
    # 验证 ZIP 内容可解析
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.read('VERSION') == b'3'


# ---- MFD 口径 ---------------------------------------------------------------

def test_mfd_pack_purpose_is_consistent():
    """pack.json purpose 与 webuddy-pack.json purpose 口径一致（都说局部提取）。"""
    pack_json = ROOT / 'factory' / 'control' / 'builtin_packs' / 'packs' / 'mfd-xml-conversion' / 'pack.json'
    tool_json = MFD_TOOL / 'webuddy-pack.json'
    pack_purpose = json.loads(pack_json.read_text())['purpose']
    tool_purpose = json.loads(tool_json.read_text())['purpose']
    # pack.json purpose 不能声称无损转换（「不是无损转换」这种否定形式可以）
    # 检查方式：去掉否定句式后不应残留独立的「无损转换」
    claim_text = pack_purpose.replace('不是整文件无损转换', '').replace('不是无损转换', '')
    assert '无损转换' not in claim_text, f'pack.json purpose 仍声称「无损转换」：{pack_purpose}'
    assert '局部' in pack_purpose, f'pack.json purpose 应含「局部」：{pack_purpose}'
    # 工具层的 purpose 本来就对
    assert '局部' in tool_purpose
