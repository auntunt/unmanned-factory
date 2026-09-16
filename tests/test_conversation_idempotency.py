"""Reliability: idempotent conversation creation and content-addressed attachments,
so a lost response on retry never spawns a duplicate conversation or drops material.
"""
import io

from tests.test_control_app import login
from tests.test_workbench_app import app_env  # noqa: F401


def _agent(client, headers):
    return client.post('/api/v4/agents', json={'name': '报价助手', 'purpose': '报价'}, headers=headers).json()


def _create(client, headers, aid, key, mode='do', project_id=None):
    return client.post(f'/api/v4/agents/{aid}/conversations',
                       json={'mode': mode, 'project_id': project_id, 'client_key': key}, headers=headers)


def test_same_key_returns_same_conversation_lost_response_safe(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    aid = _agent(client, headers)['id']
    first = _create(client, headers, aid, 'op-key-abc123')
    assert first.status_code == 201
    cid = first.json()['id']
    # A retry after a lost response replays to the SAME conversation, not a new one.
    again = _create(client, headers, aid, 'op-key-abc123')
    assert again.status_code == 201 and again.json()['id'] == cid
    rows = client.get(f'/api/v4/agents/{aid}/conversations', headers=headers).json()['conversations']
    assert len([c for c in rows if c['mode'] == 'do']) == 1  # exactly one conversation


def test_same_key_different_parameters_conflicts(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    aid = _agent(client, headers)['id']
    assert _create(client, headers, aid, 'op-key-def456').status_code == 201
    # Same key, different mode -> explicit conflict, never a second conversation.
    assert _create(client, headers, aid, 'op-key-def456', mode='maintain').status_code == 409


def test_attachment_is_idempotent_by_content_but_distinguishes_edits(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    aid = _agent(client, headers)['id']
    cid = _create(client, headers, aid, 'op-key-ghi789').json()['id']

    def attach(name, content):
        return client.post(f'/api/v4/conversations/{cid}/attachments',
                           files={'file': (name, io.BytesIO(content.encode()), 'text/csv')}, headers=headers)

    a1 = attach('prices.csv', '项目,单价\n演示,100')
    a2 = attach('prices.csv', '项目,单价\n演示,100')  # same content, retry
    assert a1.status_code == 201 and a2.status_code == 201
    assert a1.json()['attachment']['id'] == a2.json()['attachment']['id']  # idempotent by content
    conv = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    assert len(conv['attachments']) == 1
    # Same name, different content is a distinct attachment, never silently skipped.
    a3 = attach('prices.csv', '项目,单价\n演示,200')
    assert a3.json()['attachment']['id'] != a1.json()['attachment']['id']
    conv2 = client.get(f'/api/v4/conversations/{cid}', headers=headers).json()
    assert len(conv2['attachments']) == 2
