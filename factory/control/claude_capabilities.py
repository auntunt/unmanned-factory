"""Explicit SDK capability policy; no inherited personal plugins or credentials."""
import ipaddress
import os
import socket
from urllib.parse import urlsplit

READ_TOOLS = ['Read', 'Glob', 'Grep']
WEB_TOOLS = ['WebSearch', 'WebFetch']
RESEARCH_AGENT = 'webuddy-research'


def effort():
    value = os.getenv('WEBUDDY_CLAUDE_EFFORT', 'medium')
    if value not in {'low', 'medium', 'high', 'xhigh', 'max'}:
        raise ValueError('WEBUDDY_CLAUDE_EFFORT must be low, medium, high, xhigh or max')
    return value


def public_document_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443):
            return False
        host = url.hostname.lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return False
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (ValueError, OSError):
        return False


def web_tool_allowed(name, data):
    if name == 'WebSearch':
        return isinstance(data.get('query'), str) and 0 < len(data['query']) <= 2000
    if name == 'WebFetch':
        return public_document_url(data.get('url'))
    return False


def researcher_allowed(data):
    return (data.get('subagent_type') == RESEARCH_AGENT
        and isinstance(data.get('prompt'), str) and 0 < len(data['prompt']) <= 16000
        and not data.get('run_in_background') and not data.get('model')
        and not data.get('isolation') and not data.get('resume'))
