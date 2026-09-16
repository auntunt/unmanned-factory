"""Process-level test worker for the chat tool chain.

It runs the REAL ``factory.control.sdk_worker.main`` — real JSONL parsing, real
``ProviderRequest`` reconstruction, real ``ConversationTools.from_binding`` in the
worker, the real session MCP server, and the real export write to the SQLite file.
Only the external model call (``claude_agent_sdk.query``) is replaced by a fake
that drives the session-bound ``calc`` and ``export`` tools exactly as a model
would over the MCP protocol. The runner and process boundary are NOT bypassed.
"""
from __future__ import annotations

import asyncio
import json
import sys


async def _drive_session_tools(server):
    import anyio
    from mcp import ClientSession
    csend, sread = anyio.create_memory_object_stream(10)
    ssend, cread = anyio.create_memory_object_stream(10)
    instance = server['instance']
    out = {}
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(instance.run, sread, ssend, instance.create_initialization_options())
        async with ClientSession(cread, csend) as session:
            await session.initialize()
            ok = await session.call_tool('calc', {
                'items': [{'name': '演示', 'unit_price': '100', 'quantity': '3'}],
                'discount_rate': '0.9'})
            total = json.loads(ok.content[0].text)['total']
            wrote = await session.call_tool('export', {
                'title': '进程报价', 'format': 'md', 'content': f'# 进程报价\n总价：{total}'})
            out['total'] = total
            out['export'] = wrote.content[0].text
        tasks.cancel_scope.cancel()
    return out


def _install_fake_model():
    import claude_agent_sdk as sdk

    class ResultMessage:  # matched by name in _run_claude's consume loop
        is_error = False
        session_id = 'proc-test'
        total_cost_usd = 0

        def __init__(self, result):
            self.result = result

    async def query(*, prompt, options):
        # A real model would pick these tools; here we invoke them deterministically
        # through the genuine session MCP server the worker registered.
        server = options.mcp_servers['session']
        info = await _drive_session_tools(server)
        yield ResultMessage(f"已导出报价单，总价 {info['total']}")

    sdk.query = query


def main() -> int:
    _install_fake_model()
    from factory.control.sdk_worker import main as real_main
    return real_main()


if __name__ == "__main__":
    raise SystemExit(main())
