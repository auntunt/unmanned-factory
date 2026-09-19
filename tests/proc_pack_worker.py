"""Process-level test worker for the attached-pack chat chain.

Runs the REAL ``factory.control.sdk_worker.main``: real JSONL, real
``ProviderRequest`` reconstruction, real ``ConversationTools.from_binding`` in the
subprocess, the real session MCP server, and the real capability-pack execution
and receipt writes against the SQLite file. Only ``claude_agent_sdk.query`` is
replaced, by a fake that drives ``attached_tools`` → ``attached_tool_doc`` →
``run_attached_tool`` over the genuine MCP protocol, the way a model would.
The runner and the process boundary are NOT bypassed.
"""
from __future__ import annotations

import json
import os


async def _drive(server):
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
            listed = await session.call_tool('attached_tools', {})
            offered = json.loads(listed.content[0].text)
            out['offered'] = offered
            if not offered:
                out['ran'] = None
            else:
                pack_id = offered[0]['pack_id']
                # A model reads the pack's own contract before building the file.
                doc = await session.call_tool('attached_tool_doc', {'pack_id': pack_id})
                out['doc_path'] = json.loads(doc.content[0].text)['path']
                ran = await session.call_tool('run_attached_tool', {
                    'pack_id': pack_id, 'content': os.environ['PACK_TEST_CONTENT'],
                    'filename': 'from-worker.csv'})
                out['ran'] = json.loads(ran.content[0].text)
                # A model that claims a file is correct must have read it back.
                outputs = out['ran'].get('outputs') or []
                if outputs:
                    listed = await session.call_tool('session_artifacts', {})
                    out['listed'] = json.loads(listed.content[0].text)
                    back = await session.call_tool('read_session_artifact', {
                        'artifact_id': outputs[0]['artifact_id']})
                    out['read'] = json.loads(back.content[0].text)
        tasks.cancel_scope.cancel()
    return out


def _install_fake_model():
    import claude_agent_sdk as sdk

    class ResultMessage:  # matched by name in _run_claude's consume loop
        is_error = False
        session_id = 'proc-pack-test'
        total_cost_usd = 0

        def __init__(self, result):
            self.result = result

    async def query(*, prompt, options):
        info = await _drive(options.mcp_servers['session'])
        ran = info.get('ran') or {}
        read = info.get('read') or {}
        yield ResultMessage('工具执行状态：' + str(ran.get('status')) +
                            '；产物数：' + str(len(ran.get('outputs') or [])) +
                            '；回读字节：' + str(read.get('total_bytes')) +
                            '；回读含B-010：' + str('B-010' in (read.get('text') or '')))

    sdk.query = query


def main() -> int:
    _install_fake_model()
    from factory.control.sdk_worker import main as real_main
    return real_main()


if __name__ == "__main__":
    raise SystemExit(main())
