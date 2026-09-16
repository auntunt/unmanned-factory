import json
from factory.control.agents import AgentStore
from factory.control.agent_manifests import ManifestStore
from factory.control.store import Store
from scripts.equip_role_skills import equip, CATALOG


def test_equipment_preserves_human_fields_compiles_and_is_idempotent(tmp_path):
    store = Store(tmp_path/'control.db')
    agents = AgentStore(store)
    manifests = ManifestStore(store)
    catalog = json.loads(CATALOG.read_text())
    originals = {}
    for name in catalog['roles']:
        a = agents.create({'name': name, 'instructions': '人签身份\n\n已有方法', 'acceptance': ['原有断言']}, 'human')
        originals[a['id']] = manifests.get(a['id'])
    first = equip(store, actor='assistant:user-request')
    assert len(first) == len(catalog['roles'])
    for item in first:
        old = originals[item['agent_id']]
        current = manifests.get(item['agent_id'])
        assert current['identity'] == old['identity']
        assert current['assertions'] == old['assertions']
        assert current['skills'][:len(old['skills'])] == old['skills']
        assert current['revision'] == old['revision']+1
        assert item['added'] == len(catalog['roles'][item['name']])
        frozen = manifests.freeze(item['agent_id'], agents.version(item['agent_id']))
        for key in catalog['roles'][item['name']]:
            assert catalog['skills'][key]['name'] in frozen['instructions']
    second = equip(store, actor='assistant:user-request')
    assert all(item['added'] == 0 for item in second)
    assert [i['revision'] for i in first] == [i['revision'] for i in second]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_manifest_audit WHERE action='manifest.equipped:2026-09'").fetchone()[0] == len(first)


def test_unmatched_role_unchanged_and_content_bounded(tmp_path):
    store = Store(tmp_path/'control.db')
    a = AgentStore(store).create({'name': '其他岗位', 'instructions': '保留'}, 'human')
    m = ManifestStore(store)
    old = m.get(a['id'])
    assert equip(store, actor='assistant:user-request') == []
    assert m.get(a['id']) == old
    catalog = json.loads(CATALOG.read_text())
    assert all(0 < len(s['instructions']) <= 4000 for s in catalog['skills'].values())
    assert all(k in catalog['skills'] for keys in catalog['roles'].values() for k in keys)
