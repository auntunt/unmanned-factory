"""Chat tools bound to one conversation and one user. The binding is fixed at
construction: the model cannot target another conversation, user or run. Exposed
to the model as mcp__session__calc and mcp__session__export. The pure methods are
directly callable so a fake model can exercise registration, validation,
invocation, result and permission without the real SDK."""
from __future__ import annotations

import json

TOOL_NAMES = frozenset(('mcp__session__calc', 'mcp__session__export'))


def binding_for(store, cid, actor_id) -> dict:
    """Server-generated, serializable binding config for crossing the process
    boundary to the SDK worker. It carries ONLY the on-disk store path plus the
    conversation and actor the server already authorized; the model never supplies
    or sees these, and cannot point the tools at another database, user or
    conversation. Reconstructed with ConversationTools.from_binding in the worker."""
    # Preserve cid/actor_id types (actor_id is often an int user id); the owner
    # check compares by equality, so coercing to str would break it. All three
    # values are JSON-serializable and survive the JSONL round-trip unchanged.
    return {'db_path': str(store.path), 'conversation_id': cid, 'actor_id': actor_id}


class ConversationTools:
    def __init__(self, store, cid, actor_id):
        self.store = store
        self.cid = cid
        self.actor_id = actor_id

    @classmethod
    def from_binding(cls, binding: dict) -> 'ConversationTools':
        """Rebuild the bound tools inside the trusted worker from binding_for's
        config. Opening the store by its server-provided path keeps the binding
        fixed: the model cannot influence which db/user/conversation is used."""
        if not isinstance(binding, dict):
            raise TypeError('conversation binding must be an object')
        try:
            db_path = binding['db_path']
            cid = binding['conversation_id']
            actor_id = binding['actor_id']
        except KeyError as exc:
            raise ValueError(f'conversation binding missing field: {exc}') from None
        from factory.control.store import Store
        return cls(Store(db_path), cid, actor_id)

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
              'discount_rate': {'type': 'string', 'description': 'Multiplier applied to subtotal, greater than 0 and at most 1. Default 1 means no discount; VIP 九折 / 10% off means 0.90, not 0.10. Use decimal strings.'}},
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
