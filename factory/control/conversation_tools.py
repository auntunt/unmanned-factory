"""Chat tools bound to one conversation and one user. The binding is fixed at
construction: the model cannot target another conversation, user or run. Exposed
to the model as mcp__session__calc and mcp__session__export. The pure methods are
directly callable so a fake model can exercise registration, validation,
invocation, result and permission without the real SDK."""
from __future__ import annotations

import json

TOOL_NAMES = frozenset(('mcp__session__calc', 'mcp__session__export'))


class ConversationTools:
    def __init__(self, store, cid, actor_id):
        self.store = store
        self.cid = cid
        self.actor_id = actor_id

    def calc(self, items, discount_rate='1'):
        """Deterministic quote arithmetic; the inputs are the source of truth."""
        from factory.control.quote_calc import quote
        return quote(items, discount_rate)  # raises ValueError on bad input

    def export(self, title, fmt, content):
        """Controlled, conversation-scoped write of a downloadable document."""
        from factory.control.agents import AgentStore
        # add_export enforces the owner check against this bound conversation.
        return AgentStore(self.store).add_export(self.cid, self.actor_id, title, fmt, content)


def create_server(tools: ConversationTools, emit):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool('calc', 'Compute a verifiable quote total from explicit line items and a discount rate. '
          'Numbers are decimal strings. Returns per-line amounts, subtotal, rate and total. '
          'Use this instead of doing money arithmetic yourself.',
          {'type': 'object', 'properties': {
              'items': {'type': 'array', 'maxItems': 500, 'items': {'type': 'object', 'properties': {
                  'name': {'type': 'string', 'maxLength': 200},
                  'unit_price': {'type': 'string'}, 'quantity': {'type': 'string'}},
                  'required': ['unit_price', 'quantity'], 'additionalProperties': False}},
              'discount_rate': {'type': 'string'}},
           'required': ['items'], 'additionalProperties': False})
    async def calc(args):
        try:
            value = tools.calc(args['items'], args.get('discount_rate', '1'))
        except (ValueError, KeyError, TypeError) as exc:
            emit('session.calc_failed', {'conversation_id': tools.cid})
            return {'content': [{'type': 'text', 'text': '计算输入无效：' + str(exc)}], 'is_error': True}
        emit('session.calc', {'conversation_id': tools.cid, 'total': value['total']})
        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}

    @tool('export', 'Save a document (md/txt/csv) to THIS conversation as a downloadable file. '
          'This is a controlled write scoped to the current conversation; it does not create a project or code run.',
          {'type': 'object', 'properties': {
              'title': {'type': 'string', 'maxLength': 200},
              'format': {'type': 'string', 'enum': ['md', 'txt', 'csv']},
              'content': {'type': 'string', 'maxLength': 200000}},
           'required': ['format', 'content'], 'additionalProperties': False})
    async def export(args):
        try:
            item = tools.export(args.get('title', ''), args['format'], args['content'])
        except (ValueError, KeyError, TypeError) as exc:
            emit('session.export_failed', {'conversation_id': tools.cid})
            return {'content': [{'type': 'text', 'text': '导出失败：' + str(exc)}], 'is_error': True}
        except PermissionError:
            return {'content': [{'type': 'text', 'text': '无权写入该会话'}], 'is_error': True}
        emit('session.export', {'conversation_id': tools.cid, 'export_id': item['id']})
        return {'content': [{'type': 'text', 'text': '已导出：' + json.dumps(item, ensure_ascii=False)}]}

    return create_sdk_mcp_server('session', tools=[calc, export])
