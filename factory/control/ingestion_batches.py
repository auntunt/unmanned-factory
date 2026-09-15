"""Partition inert source data into bounded provider calls and retain every decision."""
import json

from factory.control.skill_ingestion import validate_mapping

BATCH_CHARS = 180_000


def batches(package):
    if len(json.dumps(package, ensure_ascii=False)) <= BATCH_CHARS and len(package['skills']) <= 24:
        return [package]
    groups, current, size = [], [], 0
    skill_paths = {s['path'] for s in package['skills']}
    for file in package['files']:
        text = file.get('text', '')
        # A SKILL definition stays whole so assertion extraction retains its scope.
        pieces = [file] if file['path'] in skill_paths or not text else [
            {**file, 'text': text[offset:offset + 40000], 'text_offset': offset,
             'text_total': len(text)} for offset in range(0, len(text), 40000)]
        for piece in pieces:
            cost = len(json.dumps(piece, ensure_ascii=False))
            if current and size + cost > BATCH_CHARS // 2:
                groups.append(current); current, size = [], 0
            current.append(piece); size += cost
    if current:
        groups.append(current)
    result = []
    for group in groups:
        paths = {f['path'] for f in group}
        result.append({**package, 'partial': True, 'files': group,
            'skills': [s for s in package['skills'] if s['path'] in paths],
            'host_primitives': [p for p in package['host_primitives'] if p['path'] in paths],
            'injection_risks': [r for r in package['injection_risks'] if r['path'] in paths],
            'batch_index': len(result), 'batch_total': len(groups)})
    return result


def merge(package, mappings):
    proposal = {'identity': mappings[0]['identity']}
    for key in ('steps', 'decisions', 'dependencies', 'injection_risks', 'authorization_required'):
        values = [value for mapping in mappings for value in mapping.get(key, [])]
        proposal[key] = list({json.dumps(v, sort_keys=True, ensure_ascii=False): v for v in values}.values())
    return validate_mapping(package, proposal)
