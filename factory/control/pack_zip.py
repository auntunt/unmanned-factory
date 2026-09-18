"""工具执行包打包：生成可独立运行的 ZIP，含 CLI harness、版本标识、sha256 校验。

与 agent_packs.pack_zip 不同：那边打的是职能体模板包（SKILL.md + agent.json +
fixtures），给平台导入用的；这边打的是工具执行包（tool/ + webuddy-pack.json +
cli.py + VERSION + sha256sums.txt），给用户在终端跑的。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

# CLI harness 模板在 builtin_packs/tools/ 下，打包时从工具源目录读取。
ROOT = Path(__file__).with_name('builtin_packs')


def _builtin_cli(tool_name: str) -> bytes | None:
    """从内置工具源目录读取 CLI harness（如果存在的话）。"""
    for tools_dir in sorted((ROOT / 'tools').iterdir()):
        pack_json = tools_dir / 'webuddy-pack.json'
        if not pack_json.is_file():
            continue
        try:
            manifest = json.loads(pack_json.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if manifest.get('tool', {}).get('name') == tool_name:
            cli_path = tools_dir / 'cli.py'
            if cli_path.is_file():
                return cli_path.read_bytes()
    return None


def build_tool_zip(version: dict, files: dict[str, bytes]) -> bytes:
    """将已发布版本的工具文件打包为可独立运行的 ZIP。

    打包内容：
    - tool/ 目录下的所有工具程序文件（来自 files 参数）
    - webuddy-pack.json（来自 files 参数）
    - cli.py（从内置工具源目录读取，如果存在）
    - VERSION（写入平台版本号）
    - sha256sums.txt（包内所有文件的 sha256，供用户下载后核对）

    版本号从 version['version'] 取，这是平台在发布时分配的不可变整数。
    """
    manifest = version.get('manifest', {})
    tool_name = manifest.get('tool', {}).get('name', 'tool')
    version_number = str(version['version'])

    # 收集要打包的文件
    pack_files: dict[str, bytes] = {}

    # 工具程序文件（来自已发布版本的 files）
    for path, content in files.items():
        pack_files[path] = content

    # CLI harness（从内置工具源目录读取）
    cli_content = _builtin_cli(tool_name)
    if cli_content is not None:
        pack_files['cli.py'] = cli_content

    # VERSION 文件（写入平台版本号）
    pack_files['VERSION'] = version_number.encode('utf-8')

    # 计算每个文件的 sha256 并生成校验文件
    checksums = []
    for path in sorted(pack_files):
        digest = hashlib.sha256(pack_files[path]).hexdigest()
        checksums.append(f'{digest}  {path}')
    pack_files['sha256sums.txt'] = '\n'.join(checksums).encode('utf-8')

    # 打包成 ZIP
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(pack_files):
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, pack_files[path])

    return output.getvalue()
