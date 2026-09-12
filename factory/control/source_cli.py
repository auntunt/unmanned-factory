"""Operator-only ingestion. CLI producers pipe JSON; MCP reads explicit URIs.

Commands and credentials never enter modules, run prompts or the SDK worker.
No shell is used, no tools are automatically trusted from MCP descriptions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from factory.control.sources import SourceStore, MAX_BYTES
from factory.control.store import Store


async def read_mcp_resources(command, args, uris, env, timeout_s=30):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    if not Path(command).is_absolute() or not Path(command).is_file():
        raise ValueError('MCP 服务必须使用管理员指定的绝对可执行文件路径')
    if not uris or len(uris) > 100 or len(set(uris)) != len(uris):
        raise ValueError('指定 1 到 100 个不重复的资源 URI')
    documents, size = [], 0
    # stderr is not forwarded: third-party servers can print their credentials.
    with open(os.devnull, 'w') as errlog:
        async with asyncio.timeout(timeout_s):
            async with stdio_client(StdioServerParameters(command=command, args=args, env=env), errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    for uri in uris:
                        resource = await session.read_resource(uri)
                        contents = getattr(resource, 'contents', None)
                        if not contents:
                            raise ValueError('MCP 未返回文本资源')
                        chunks = []
                        for content in contents:
                            if not isinstance(getattr(content, 'text', None), str):
                                raise ValueError('仅支持文本资源；请先通过受信任转换器处理二进制文件')
                            chunks.append(content.text)
                        text = '\n'.join(chunks)
                        size += len(text.encode())
                        if size > MAX_BYTES:
                            raise ValueError('MCP 资源超出导入限额')
                        documents.append({'id': str(len(documents) + 1), 'title': uri[:200], 'uri': uri, 'text': text})
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import reviewed project references; no model calls or remote writes.')
    parser.add_argument('--db', required=True, help='Existing Webuddy control.db')
    parser.add_argument('--project', required=True)
    parser.add_argument('--actor', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('import-json', 'import-mcp'):
        p = sub.add_parser(action)
        p.add_argument('--name', required=True)
        p.add_argument('--source-id')
        p.add_argument('--expected-revision', type=int, default=0)
        if action == 'import-json':
            p.add_argument('--file', required=True, help='JSON {documents:[{id,title,text,uri}]} or - for stdin')
        else:
            p.add_argument('--command', required=True, help='Trusted installed MCP stdio executable; never a workspace script')
            p.add_argument('--arg', action='append', default=[])
            p.add_argument('--uri', action='append', required=True)
            p.add_argument('--env-name', action='append', default=[], help='Explicit environment variable to pass, never its value')
    sub.add_parser('list')
    bind = sub.add_parser('bind')
    bind.add_argument('--slot', required=True)
    bind.add_argument('--source-id', required=True)
    bind.add_argument('--source-revision', type=int, required=True)
    bind.add_argument('--expected-revision', type=int, default=0)
    for action in ('enable', 'disable'):
        p = sub.add_parser(action)
        p.add_argument('--source-id', required=True)
    args = parser.parse_args(argv)
    if not Path(args.db).is_file():
        parser.error('Database does not exist; refusing to create an unintended control database')
    try:
        sources = SourceStore(Store(args.db))
        if args.action == 'list':
            result = {'sources': sources.list(args.project)}
        elif args.action == 'bind':
            result = sources.bind(args.project, args.slot, {'id': args.source_id, 'revision': args.source_revision}, args.expected_revision, args.actor)
        elif args.action in ('enable', 'disable'):
            sources.enable(args.project, args.source_id, args.action == 'enable', args.actor)
            result = {'source_id': args.source_id, 'enabled': args.action == 'enable'}
        else:
            if args.action == 'import-json':
                if args.file == '-':
                    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
                else:
                    with open(args.file, 'rb') as stream:
                        raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('导入 JSON 超出 200 KB')
                body = json.loads(raw)
                if not isinstance(body, dict) or set(body) != {'documents'}:
                    raise ValueError('导入结构必须是 {documents: [...]}')
                documents = body['documents']
                transport = 'cli' if args.file == '-' else 'json'
            else:
                env = {}
                for key in args.env_name:
                    if key not in os.environ:
                        raise ValueError('指定的环境变量未配置')
                    env[key] = os.environ[key]
                documents = asyncio.run(read_mcp_resources(args.command, args.arg, args.uri, env))
                transport = 'mcp-resource'
            result = sources.put(args.project, name=args.name, documents=documents, actor=args.actor,
                                 sid=args.source_id, expected_revision=args.expected_revision, transport=transport)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # No raw SDK/process exception, URI, request payload or credential echo.
        print(json.dumps({'error': '数据源操作失败，未报告成功', 'error_type': type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
