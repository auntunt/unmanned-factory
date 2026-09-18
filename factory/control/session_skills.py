"""会话级 Skill 绑定：仅在当前会话生效，不进团队能力库。

session_id 对应 agent_conversations.id（工作会话主键）。
存储独立表 session_skills，不复用 instruction_modules / skill_assets / project_modules。
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from pathlib import PurePosixPath

from factory.control.agents import inspect_skill, MAX_PACK_FILES, macos_junk
from factory.control.store import now


class SessionSkillStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS session_skills(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS session_skills_session
                    ON session_skills(session_id);
            ''')

    def list(self, session_id: str) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT data FROM session_skills WHERE session_id=? ORDER BY rowid',
                (session_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, skill_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute(
                'SELECT data FROM session_skills WHERE id=?',
                (skill_id,)).fetchone()
        if not row:
            raise KeyError(skill_id)
        return json.loads(row[0])

    def create(self, session_id: str, raw: bytes, actor_id: str) -> dict:
        """从 ZIP 包导入一个 session skill 绑定。

        不触碰 instruction_modules / skill_assets / project_modules / agent manifests。
        不授予任何工具权限、凭据或部署权限。
        """
        package = _parse_zip(raw)
        # 取第一个 skill 作为绑定入口
        skill_info = package['skills'][0] if package['skills'] else {}
        name = skill_info.get('name', 'unnamed')
        description = skill_info.get('description', '')
        entry = skill_info.get('path', '')

        # 依赖检测：检查包声明的依赖
        dependencies = _extract_dependencies(package)
        dependency_state = 'ready' if not dependencies else 'missing'

        sid = uuid.uuid4().hex
        at = now()
        data = {
            'id': sid,
            'session_id': session_id,
            'name': name,
            'origin': 'zip',
            'source_ref': entry,
            'source_version': skill_info.get('version', ''),
            'source_sha256': package['sha256'],
            'entry': entry,
            'description': description,
            'dependencies': dependencies,
            'import_state': 'imported',
            'dependency_state': dependency_state,
            'actor_id': actor_id,
            'created_at': at,
        }
        with self.store.connect() as db:
            db.execute(
                'INSERT INTO session_skills(id, session_id, data, created_at) VALUES (?,?,?,?)',
                (sid, session_id, json.dumps(data, ensure_ascii=False), at))
        return data

    def create_from_data(self, session_id: str, payload: dict, actor_id: str) -> dict:
        """直接从结构化数据创建绑定（供内部或兼容调用）。"""
        sid = uuid.uuid4().hex
        at = now()
        origin = payload.get('origin', 'github')
        data = {
            'id': sid,
            'session_id': session_id,
            'name': payload.get('name', 'unnamed'),
            'origin': origin,
            'source_ref': payload.get('source_ref', ''),
            'source_version': payload.get('source_version', ''),
            'source_sha256': payload.get('source_sha256', ''),
            'entry': payload.get('entry', ''),
            'description': payload.get('description', ''),
            'dependencies': payload.get('dependencies', []),
            'import_state': payload.get('import_state', 'imported'),
            'dependency_state': payload.get('dependency_state', 'unknown'),
            'actor_id': actor_id,
            'created_at': at,
        }
        with self.store.connect() as db:
            db.execute(
                'INSERT INTO session_skills(id, session_id, data, created_at) VALUES (?,?,?,?)',
                (sid, session_id, json.dumps(data, ensure_ascii=False), at))
        return data

    def create_from_github(self, session_id: str, fetch_result, actor_id: str) -> dict:
        """从 GitHub 拉取结果创建 session skill 绑定。

        fetch_result 是 github_skill_fetch.FetchResult。
        不触碰 instruction_modules / skill_assets / project_modules / agent manifests。
        不授予任何工具权限、凭据或部署权限。
        """
        dependencies = list(fetch_result.dependencies)
        dependency_state = 'ready' if not dependencies else 'missing'

        return self.create_from_data(session_id, {
            'name': fetch_result.name,
            'origin': 'github',
            'source_ref': fetch_result.source_ref,
            'source_version': fetch_result.commit_sha,
            'source_sha256': fetch_result.content_sha256,
            'entry': fetch_result.entry,
            'description': fetch_result.description,
            'dependencies': dependencies,
            'import_state': 'imported',
            'dependency_state': dependency_state,
        }, actor_id)

    def delete(self, skill_id: str, session_id: str) -> None:
        """解绑。已产生的 run 记录与其 session_skill_snapshot 不受影响。"""
        with self.store.connect() as db:
            row = db.execute(
                'SELECT data FROM session_skills WHERE id=? AND session_id=?',
                (skill_id, session_id)).fetchone()
            if not row:
                raise KeyError(skill_id)
            db.execute(
                'DELETE FROM session_skills WHERE id=? AND session_id=?',
                (skill_id, session_id))

    def freeze(self, session_id: str) -> list[dict]:
        """快照当前会话绑定，用于注入 run 的 session_skill_snapshot。"""
        return self.list(session_id)


def _parse_zip(raw: bytes) -> dict:
    """最小化 ZIP 解析：提取 skill 元数据和 SHA256，不执行不提取到磁盘。"""
    try:
        inspect_skill(raw, max_files=MAX_PACK_FILES)
    except ValueError:
        raise

    import yaml
    sha256 = hashlib.sha256(raw).hexdigest()
    skills = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in sorted(archive.infolist(), key=lambda e: e.filename):
            if item.is_dir() or macos_junk(item.filename):
                continue
            if PurePosixPath(item.filename.replace('\\', '/')).suffix.lower() != '.md':
                continue
            content = archive.read(item)
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                continue
            # 检查是否为 SKILL.md 或带 name frontmatter 的 Markdown
            import re
            metadata = {}
            match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
            if match:
                try:
                    metadata = yaml.safe_load(match.group(1)) or {}
                except yaml.YAMLError:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
            is_skill_md = PurePosixPath(item.filename.replace('\\', '/')).name.lower() == 'skill.md'
            if not is_skill_md and 'name' not in metadata:
                continue
            name = metadata.get('name',
                                PurePosixPath(item.filename.replace('\\', '/')).parent.name or 'root')
            skills.append({
                'path': item.filename,
                'name': name if isinstance(name, str) else str(name),
                'description': str(metadata.get('description', '')),
                'version': str(metadata.get('version', '')),
                'sha256': hashlib.sha256(content).hexdigest(),
                'dependencies': metadata.get('dependencies', []),
            })
    if not skills:
        raise ValueError('包内没有 SKILL.md 或带 name frontmatter 的 Markdown skill')
    return {'sha256': sha256, 'skills': skills}


def _extract_dependencies(package: dict) -> list[str]:
    """从包中提取声明的依赖列表。"""
    deps = []
    for skill in package.get('skills', []):
        raw_deps = skill.get('dependencies', [])
        if isinstance(raw_deps, list):
            for d in raw_deps:
                if isinstance(d, str) and d not in deps:
                    deps.append(d)
    return deps
