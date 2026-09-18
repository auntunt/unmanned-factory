"""GitHub Skill 拉取层：从 GitHub 仓库拉取 Skill 内容。

可注入（构造时传入 client），便于确定性测试。
来源内容是数据不是指令：拉取的文件内容不改变平台规则/权限/配置。
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

import httpx

OWNER_REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
# 单文件上限 2 MiB（与 ZIP 路径一致）
MAX_FILE_SIZE = 2 * 1024 * 1024


@dataclass
class FetchResult:
    """GitHub Skill 拉取结果。"""
    commit_sha: str            # 40 位 hex
    source_ref: str            # 用户给的原始引用（owner/repo#ref/subpath）
    content_sha256: str        # 取回内容的 SHA256
    entry: str                 # SKILL.md 入口路径
    name: str                  # skill 名称
    description: str           # skill 描述
    version: str               # skill 版本
    dependencies: list[str] = field(default_factory=list)


class GitHubSkillFetchError(Exception):
    """GitHub Skill 拉取失败，携带结构化错误信息。"""
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class GitHubSkillFetcher:
    """GitHub Skill 拉取器。

    构造时可注入 httpx.Client（用于测试），也可通过 token 参数或
    FACTORY_GITHUB_TOKEN 环境变量配置认证。没有 token 时只能访问公开仓库。
    """

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token if token is not None else os.environ.get('FACTORY_GITHUB_TOKEN', '')
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            }
            if self.token:
                headers['Authorization'] = f'Bearer {self.token}'
            self._client = httpx.Client(
                base_url='https://api.github.com',
                timeout=30,
                headers=headers,
            )
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()

    def resolve_commit_sha(self, owner_repo: str, ref: str | None = None) -> str:
        """解析引用到 40 位 commit SHA。

        ref 可以是分支名、标签名或 SHA。为空时使用仓库默认分支。
        使用 GET /repos/{owner}/{repo}/commits/{ref} 获取 commit 信息，
        从 JSON 响应的 sha 字段提取 40 位 hex。
        """
        if not OWNER_REPO_RE.fullmatch(owner_repo):
            raise GitHubSkillFetchError(
                f'无效 GitHub 仓库名: {owner_repo}',
                'invalid_repository')

        # 如果 ref 已经是 40 位 SHA，直接验证它存在
        if ref and SHA_RE.fullmatch(ref):
            return self._fetch_commit_sha(owner_repo, ref)

        target = ref or 'HEAD'
        return self._fetch_commit_sha(owner_repo, target)

    def _fetch_commit_sha(self, owner_repo: str, ref: str) -> str:
        """通过 GitHub API 获取 commit SHA。

        先尝试 SHA 媒体类型（轻量），失败则回退到 JSON 响应。
        """
        try:
            resp = self.client.get(
                f'/repos/{owner_repo}/commits/{ref}',
                headers={'Accept': 'application/vnd.github.sha'})
            resp.raise_for_status()
            sha = resp.text.strip()
            if SHA_RE.fullmatch(sha):
                return sha
        except httpx.HTTPStatusError as exc:
            # 422/415 等：可能不支持 SHA 媒体类型，回退到 JSON
            if exc.response.status_code in (401, 403, 404):
                self._raise_for_status(exc, owner_repo)

        # 回退：用标准 JSON 响应
        try:
            resp = self.client.get(f'/repos/{owner_repo}/commits/{ref}')
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                sha = data.get('sha', '')
                if SHA_RE.fullmatch(sha):
                    return sha
        except httpx.HTTPStatusError as exc:
            self._raise_for_status(exc, owner_repo)

        raise GitHubSkillFetchError(
            f'无法解析引用 {ref} 到 commit SHA',
            'invalid_ref')

    def fetch_skill_md(self, owner_repo: str, sha: str,
                       subpath: str | None = None) -> tuple[str, bytes]:
        """拉取 SKILL.md 内容。

        Returns:
            (entry_path, content_bytes)
        """
        # 构建搜索路径
        if subpath:
            # 去掉前后斜杠
            subpath = subpath.strip('/')
            if subpath.lower().endswith('.md'):
                # 直接指定了文件
                skill_path = subpath
            else:
                skill_path = f'{subpath}/SKILL.md'
        else:
            skill_path = 'SKILL.md'

        try:
            resp = self.client.get(
                f'/repos/{owner_repo}/contents/{skill_path}',
                params={'ref': sha})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # 如果指定了子路径但不是 .md，也尝试列出目录找 SKILL.md
                if subpath and not subpath.lower().endswith('.md'):
                    raise GitHubSkillFetchError(
                        f'仓库 {owner_repo} 路径 {skill_path} 不存在',
                        'skill_not_found')
                raise GitHubSkillFetchError(
                    f'仓库 {owner_repo} 中未找到 {skill_path}',
                    'skill_not_found')
            self._raise_for_status(exc, owner_repo)
            # unreachable but makes type checker happy
            raise  # pragma: no cover

        data = resp.json()
        if not isinstance(data, dict) or data.get('type') != 'file':
            raise GitHubSkillFetchError(
                f'{skill_path} 不是文件', 'not_a_file')

        size = data.get('size', 0)
        if size > MAX_FILE_SIZE:
            raise GitHubSkillFetchError(
                f'文件 {skill_path} 大小 {size} 超过 {MAX_FILE_SIZE} 字节限制',
                'file_too_large')

        # GitHub Contents API returns base64-encoded content
        import base64
        content_b64 = data.get('content', '')
        try:
            content = base64.b64decode(content_b64)
        except Exception:
            raise GitHubSkillFetchError(
                f'无法解码 {skill_path} 内容', 'decode_error')

        return skill_path, content

    def fetch(self, owner_repo: str, ref: str | None = None,
              subpath: str | None = None) -> FetchResult:
        """完整拉取流程：解析引用、拉取内容、解析元数据。

        Returns:
            FetchResult 包含解析后的 skill 元数据。

        Raises:
            GitHubSkillFetchError: 拉取或解析失败。
        """
        # 1. 验证仓库名格式
        if not OWNER_REPO_RE.fullmatch(owner_repo):
            raise GitHubSkillFetchError(
                f'无效 GitHub 仓库名: {owner_repo}',
                'invalid_repository')

        # 2. 构造 source_ref（用户给的原始引用）
        source_ref = owner_repo
        if ref:
            source_ref += f'#{ref}'
        if subpath:
            source_ref += f'/{subpath.strip("/")}'

        # 3. 解析 ref → commit SHA
        sha = self.resolve_commit_sha(owner_repo, ref)

        # 4. 拉取 SKILL.md 内容
        entry_path, content = self.fetch_skill_md(owner_repo, sha, subpath)

        # 5. 解析 SKILL.md frontmatter（同 ZIP 路径的解析逻辑）
        skill_info = _parse_skill_content(entry_path, content)
        if skill_info is None:
            raise GitHubSkillFetchError(
                f'{entry_path} 不是有效的 Skill 文件（缺少 name frontmatter 或不是 SKILL.md）',
                'invalid_skill_format')

        content_sha256 = hashlib.sha256(content).hexdigest()

        return FetchResult(
            commit_sha=sha,
            source_ref=source_ref,
            content_sha256=content_sha256,
            entry=entry_path,
            name=skill_info['name'],
            description=skill_info['description'],
            version=skill_info['version'],
            dependencies=skill_info['dependencies'],
        )

    @staticmethod
    def _raise_for_status(exc: httpx.HTTPStatusError, owner_repo: str):
        code = exc.response.status_code
        if code == 401:
            raise GitHubSkillFetchError(
                'GitHub 凭据无效或已过期', 'auth_invalid')
        elif code == 403:
            raise GitHubSkillFetchError(
                f'GitHub 拒绝访问仓库 {owner_repo}，请检查权限',
                'access_denied')
        elif code == 404:
            raise GitHubSkillFetchError(
                f'GitHub 仓库 {owner_repo} 不存在或不可见',
                'repo_not_found')
        else:
            raise GitHubSkillFetchError(
                f'GitHub API 返回 HTTP {code}',
                'api_error')


def _parse_skill_content(filename: str, content: bytes) -> dict | None:
    """解析单个 SKILL.md 文件的 frontmatter。

    与 session_skills._parse_zip 中对单文件的解析逻辑一致。
    返回 None 表示该文件不是有效 skill 文件。
    """
    import re
    from pathlib import PurePosixPath
    import yaml

    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        return None

    metadata = {}
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if match:
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}

    normalized = filename.replace('\\', '/')
    is_skill_md = PurePosixPath(normalized).name.lower() == 'skill.md'
    if not is_skill_md and 'name' not in metadata:
        return None

    name = metadata.get('name',
                        PurePosixPath(normalized).parent.name or 'root')
    dependencies = metadata.get('dependencies', [])
    if not isinstance(dependencies, list):
        dependencies = []
    dependencies = [str(d) for d in dependencies if isinstance(d, str)]

    return {
        'name': name if isinstance(name, str) else str(name),
        'description': str(metadata.get('description', '')),
        'version': str(metadata.get('version', '')),
        'dependencies': dependencies,
    }
