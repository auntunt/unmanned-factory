"""Independent review: valid Chinese text must remain readable across page boundaries."""
from tests.test_chat_attached_tool import bound_chat, app_env, _actor_id, _register_text_artifact
from factory.control.conversation_pack_tools import PackTools, READ_MAX_BYTES


def test_default_page_preserves_chinese_utf8_and_can_continue(bound_chat):
    client, store, headers, pack, version, aid, cid = bound_chat
    original = '会议纪要：待确认。🙂' * 3000
    artifact = _register_text_artifact(store, cid, _actor_id(store, cid), '会议纪要.md', original)
    reader = PackTools(store, cid, _actor_id(store, cid), actor_role='admin')
    offset = 0
    chunks = []
    while True:
        page = reader.read_artifact(artifact, offset=offset)
        assert page['offset'] == offset
        assert 0 < page['returned_bytes'] <= READ_MAX_BYTES
        assert len(page['text'].encode('utf-8')) == page['returned_bytes']
        chunks.append(page['text'])
        offset += page['returned_bytes']
        if not page['truncated']:
            break
    assert ''.join(chunks) == original
    assert offset == len(original.encode('utf-8'))
