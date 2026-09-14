"""Database-backed, versioned operation catalog; seed files are migrations only."""
import hashlib
import json
from pathlib import Path


class OperationStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operation_presets(id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(id,version))")
            for row in json.loads((Path(__file__).parent / 'templates/operations-v1.json').read_text()):
                db.execute('INSERT OR IGNORE INTO operation_presets VALUES(?,?,?)',
                           (row['id'], row['version'], json.dumps(row, ensure_ascii=False)))

    def list(self):
        with self.store.connect() as db:
            return [{**json.loads(r['data']), 'id': r['id'], 'version': r['version']} for r in db.execute("SELECT id,version,data FROM operation_presets p WHERE version=(SELECT MAX(version) FROM operation_presets WHERE id=p.id) ORDER BY rowid")]

    def get(self, kind):
        for row in self.list():
            if row['id'] == kind:
                return row
        raise ValueError('未知工作类型')

    def compile(self, kind, description, fields=None):
        preset = self.get(kind)
        fields = fields or {}
        allowed = {f['id']: f['label'] for f in preset['fields']}
        if set(fields) - set(allowed):
            raise ValueError('工作类型不支持这些补充字段')
        if any(not isinstance(v, str) or len(v) > 8000 for v in fields.values()):
            raise ValueError('补充字段必须为不超过 8000 字符的文字')
        fields = {k: v.strip() for k, v in fields.items() if v.strip()}
        # Preserve pre-upgrade fingerprints for field-free submissions.
        identity = f'{kind}\0{description}'
        if fields:
            identity += '\0' + json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        compiled = description
        if fields:
            compiled += '\n\n补充信息：\n' + '\n'.join(f'{allowed[k]}：{v}' for k, v in fields.items())
        if kind != 'general':
            compiled += (f"\n\n[项目工作方式 · {kind} · v{preset['version']}]\n{preset['brief']}\n"
                '沿用项目既有权限、能力挂载和验收规则；优先复用有效成果。交付包含实际改动、验证证据、尚未解决的事项；需要额外授权或缺少连接时说明具体阻碍。')
        return compiled, fingerprint, preset


def operation_results(verdict, ledger):
    """Bind maintenance summaries to this review's actual criterion evidence."""
    rows = {row['id']: row for row in ledger['items']}
    result = {}
    supplied = verdict.get('operation_results')
    supplied = supplied if isinstance(supplied, dict) else {}
    for key in ('regression', 'startup_command', 'health', 'build_artifacts', 'rollback'):
        value = supplied.get(key)
        if not isinstance(value, dict) or value.get('status') not in ('pass', 'fail', 'unverified'):
            continue
        refs = value.get('criterion_ids')
        evidence = value.get('evidence')
        if (not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in rows for r in refs)
                or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 3000):
            continue
        statuses = [rows[r]['status'] for r in refs]
        status = 'fail' if 'fail' in statuses else 'unverified' if 'unverified' in statuses else 'pass'
        if value.get('status') == 'unverified':
            status = 'unverified'
        elif value.get('status') == 'fail':
            status = 'fail'
        result[key] = {'status': status, 'evidence': evidence, 'criterion_ids': refs}
    return result
