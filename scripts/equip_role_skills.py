"""Explicit audited role enrichment; never signs external packages or changes identity."""
import argparse
import json
from pathlib import Path
from factory.control.agent_manifests import ManifestStore
from factory.control.store import Store

CATALOG = Path(__file__).resolve().parents[1] / 'docs/skill-expansion-2026-09.json'


def equip(store, *, actor):
    data = json.loads(CATALOG.read_text())
    manifests = ManifestStore(store)
    with store.connect() as db:
        agents = [json.loads(r[0]) for r in db.execute('SELECT data FROM agents')]
    result = []
    for agent in agents:
        keys = data['roles'].get(agent['name'])
        if not keys:
            continue
        old = manifests.get(agent['id'])
        existing = manifests.resolve(old)
        refs = list(old['skills'])
        for key in keys:
            payload = data['skills'][key]
            match = next((s for s in existing if all(s.get(k) == v for k, v in payload.items())), None)
            if match is None:
                match = manifests.modules.save(payload, actor)
                refs.append({'id': match['id'], 'version': match['version']})
        new = old
        if refs != old['skills']:
            new = manifests.save(agent['id'], {k: old[k] for k in ('identity', 'assertions')} | {'skills': refs},
                                 old['revision'], actor, human=False, action='manifest.equipped:2026-09')
        result.append({'agent_id': agent['id'], 'name': agent['name'], 'revision': new['revision'],
                       'skills': len(new['skills']), 'added': len(refs)-len(old['skills'])})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--actor', required=True)
    args = parser.parse_args()
    print(json.dumps(equip(Store(args.database), actor=args.actor), ensure_ascii=False, indent=2))
