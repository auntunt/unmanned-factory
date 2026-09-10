"""Exercise the installed SDK's real NDJSON parser, without a model or subprocess."""
import asyncio
import json

import pytest

from factory.control.providers import _CLAUDE_MAX_BUFFER_SIZE


def messages(size, limit):
    sdk = pytest.importorskip('claude_agent_sdk')
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    options = sdk.ClaudeAgentOptions(max_buffer_size=limit)
    transport = SubprocessCLITransport('unused', options)
    wire = json.dumps({'type': 'user', 'message': {'content': [
        {'type': 'image', 'source': {'type': 'base64', 'data': 'x' * size}}]}}) + '\n'
    class Process:
        async def wait(self): return 0
    async def chunks():
        for offset in range(0, len(wire), 65536):
            yield wire[offset:offset + 65536]
    transport._process = Process()
    transport._stdout_stream = chunks()
    async def consume():
        return [item async for item in transport.read_messages()]
    return asyncio.run(consume())


def test_default_buffer_reproduces_large_screenshot_failure():
    sdk = pytest.importorskip('claude_agent_sdk')
    with pytest.raises(sdk.CLIJSONDecodeError, match='maximum buffer size of 1048576'):
        messages(2 * 1024 * 1024, None)


def test_configured_buffer_accepts_message_above_old_limit():
    result = messages(2 * 1024 * 1024, _CLAUDE_MAX_BUFFER_SIZE)
    assert len(result[0]['message']['content'][0]['source']['data']) == 2 * 1024 * 1024
    assert _CLAUDE_MAX_BUFFER_SIZE == 16 * 1024 * 1024


def test_configured_buffer_still_rejects_oversized_message():
    sdk = pytest.importorskip('claude_agent_sdk')
    with pytest.raises(sdk.CLIJSONDecodeError, match='maximum buffer size of 16777216'):
        messages(_CLAUDE_MAX_BUFFER_SIZE + 1, _CLAUDE_MAX_BUFFER_SIZE)
