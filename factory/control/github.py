"""Signed Issue intake and idempotent verified-branch delivery to GitHub."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx

REPOSITORY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')
SHA = re.compile(r'^[0-9a-f]{40}$')
PR_PATH = re.compile(r'^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)$')


def _repository(value: object) -> str:
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value):
        raise ValueError('无效 GitHub 仓库名')
    return value


def _same_repository(left: object, right: str) -> bool:
    return isinstance(left, str) and left.casefold() == right.casefold()


def _pr_url_number(url: object, repository: str) -> int:
    """Return a PR number only for a canonical github.com URL in repository."""
    if not isinstance(url, str):
        raise ValueError('GitHub PR URL 无效')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc.casefold() != 'github.com'
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment):
        raise ValueError('GitHub PR URL 无效')
    match = PR_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError('GitHub PR URL 无效')
    url_repository = f'{match.group(1)}/{match.group(2)}'
    if not _same_repository(url_repository, repository):
        raise ValueError('GitHub PR URL 仓库不匹配')
    return int(match.group(3))


def _number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('GitHub PR 编号无效')
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f'无效{label} SHA')
    return value


def _pr_number(payload: dict, repository: str, *, allow_url_fallback: bool) -> int:
    number = payload.get('number')
    if number is not None:
        number = _number(number)
        if payload.get('html_url') is not None:
            url_number = _pr_url_number(payload['html_url'], repository)
            if url_number != number:
                raise ValueError('GitHub PR 编号与 URL 不一致')
        return number
    if allow_url_fallback and payload.get('html_url') is not None:
        return _pr_url_number(payload['html_url'], repository)
    raise ValueError('GitHub PR 响应缺少 PR 编号')


def _canonical_pr_url(payload: dict, repository: str, number: int, fallback: object = None) -> str:
    url = payload.get('html_url', fallback)
    url_number = _pr_url_number(url, repository)
    if url_number != number:
        raise ValueError('GitHub PR URL 与编号不一致')
    return url


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature.startswith('sha256='):
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubDelivery:
    def __init__(self, token: str, client=None):
        self.token = token
        self.client = client or httpx.Client(base_url='https://api.github.com', timeout=30,
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'})

    def close(self):
        self.client.close()

    def account(self):
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        response = self.client.get('/user')
        response.raise_for_status()
        user = response.json()
        login = user.get('login') if isinstance(user, dict) else None
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', login):
            raise ValueError('GitHub 账户响应无效')
        return {'login': login, 'html_url': f'https://github.com/{login}',
                'avatar_url': user.get('avatar_url')}

    @staticmethod
    def _repo_view(value, *, require_push=True):
        if not isinstance(value, dict):
            raise ValueError('GitHub 仓库响应无效')
        full_name = _repository(value.get('full_name'))
        if require_push and not (value.get('permissions') or {}).get('push'):
            raise ValueError('GitHub 仓库没有写入权限')
        if value.get('archived') or value.get('disabled'):
            raise ValueError('GitHub 仓库已归档或停用')
        if type(value.get('id')) is not int or value['id'] <= 0:
            raise ValueError('GitHub 仓库编号无效')
        return {'id': value['id'], 'full_name': full_name, 'name': full_name.split('/')[1],
                'private': bool(value.get('private')), 'default_branch': value.get('default_branch') or 'main',
                'html_url': f'https://github.com/{full_name}', 'permissions': {'push': True}}

    def repositories(self, *, page=1, per_page=50):
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        if type(page) is not int or page < 1 or type(per_page) is not int or not 1 <= per_page <= 100:
            raise ValueError('GitHub 仓库分页无效')
        response = self.client.get('/user/repos', params={'page': page, 'per_page': per_page,
            'sort': 'updated', 'direction': 'desc', 'affiliation': 'owner,collaborator,organization_member'})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError('GitHub 仓库列表响应无效')
        writable = [self._repo_view(row) for row in rows if isinstance(row, dict)
            and (row.get('permissions') or {}).get('push') and not row.get('archived') and not row.get('disabled')]
        return {'repositories': writable, 'page': page, 'has_more': len(rows) == per_page}

    def repository(self, full_name):
        repo = _repository(full_name)
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        response = self.client.get(f'/repos/{repo}')
        response.raise_for_status()
        result = self._repo_view(response.json())
        if not _same_repository(result['full_name'], repo):
            raise ValueError('GitHub 仓库身份已变化，请重新选择')
        return result

    def create_repository(self, *, name, private=True, description=''):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name):
            raise ValueError('GitHub 仓库名称只能使用字母、数字、连字符、下划线和点，最多 100 字符')
        if type(private) is not bool or not isinstance(description, str) or len(description) > 350:
            raise ValueError('GitHub 仓库配置无效')
        owner = self.account()['login']
        expected = f'{owner}/{name}'
        # A prior successful POST with a lost response is ambiguous. Never infer
        # ownership of a creation attempt from a matching name alone: select the
        # existing repo explicitly, or use the caller's durable success receipt.
        existing = self.client.get(f'/repos/{expected}')
        if existing.status_code != 404:
            existing.raise_for_status()
            raise ValueError('GitHub 同名仓库已存在，请选择已有仓库；不会覆盖或重新创建')
        response = self.client.post('/user/repos', json={'name': name, 'private': private,
            'description': description, 'auto_init': False})
        response.raise_for_status()
        result = self._repo_view(response.json(), require_push=False)
        if not _same_repository(result['full_name'], expected):
            raise ValueError('GitHub 新仓库身份不匹配，请核对账户后选择已有仓库')
        return result

    def _git_env(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        header = base64.b64encode(f'x-access-token:{self.token}'.encode()).decode()
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
            GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {header}',
            GIT_CONFIG_KEY_1='core.hooksPath', GIT_CONFIG_VALUE_1='/dev/null')
        return env

    @staticmethod
    def _git(root, args, env, *, timeout=120):
        result = subprocess.run(['git', *args], cwd=root, env=env,
            capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise ValueError(push_failure_message(result.stderr))
        return result.stdout.strip()

    def publish(self, project: dict, run: dict) -> dict:
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        repo = _repository(project['repository'])
        artifacts = run['artifacts']
        branch, sha = artifacts['branch'], artifacts['commit']
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('无效验收提交 SHA')
        if not isinstance(branch, str) or not re.fullmatch(r'factory/[A-Za-z0-9_-]+', branch):
            raise ValueError('拒绝发布非工厂交付分支')
        root = Path(artifacts['worktree'])
        current = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
            capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=root,
            capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        if current != sha or dirty:
            raise ValueError('验收后工作区已变化，拒绝发布；请重新验证')
        env = self._git_env()
        checked_branch = self._git(root, ['symbolic-ref', '--short', 'HEAD'], env, timeout=30)
        if checked_branch != branch:
            raise ValueError('验收后工作区分支已变化，拒绝发布；请重新验证')
        base = project.get('github_base_branch') or project.get('base_branch', 'main')
        if not isinstance(base, str) or not base or base.startswith('-'):
            raise ValueError('无效 GitHub 基础分支')
        self._git(root, ['check-ref-format', '--branch', base], env, timeout=30)
        remote = f'https://github.com/{repo}.git'
        refs_text = self._git(root, ['ls-remote', '--refs', remote], env)
        refs = {}
        for line in refs_text.splitlines():
            fields = line.split('\t')
            if len(fields) != 2 or not SHA.fullmatch(fields[0]):
                raise ValueError('GitHub 远端引用响应无效')
            refs[fields[1]] = fields[0]
        base_ref = f'refs/heads/{base}'
        if not refs or refs.get(base_ref) == sha:
            if not refs:
                # No force flags: a concurrent incompatible initial push is rejected.
                self._git(root, ['push', remote, f'{sha}:{base_ref}'], env)
            observed = self._git(root, ['ls-remote', '--refs', remote, base_ref], env)
            if observed != f'{sha}\t{base_ref}':
                raise ValueError('GitHub 首次上传后的提交不匹配，请重新核对远端状态')
            return {'publication_type': 'initial', 'repository_url': f'https://github.com/{repo}',
                    'repository': repo, 'commit': sha, 'branch': branch, 'published_branch': base}
        remote_base = refs.get(base_ref)
        if not remote_base:
            raise ValueError('GitHub 基础分支不存在，请选择仓库的实际默认分支')
        self._git(root, ['fetch', '--no-tags', '--no-write-fetch-head', remote, base_ref], env)
        relation = subprocess.run(['git', 'merge-base', sha, remote_base], cwd=root, env=env,
            capture_output=True, text=True, timeout=30)
        if relation.returncode or not SHA.fullmatch(relation.stdout.strip()):
            raise ValueError('GitHub 仓库与当前成果没有共同历史，请选择空仓库或创建新仓库；不会覆盖已有项目')
        self._git(root, ['push', remote, f'{sha}:refs/heads/{branch}'], env)
        owner = repo.split('/')[0]
        response = self.client.get(f'/repos/{repo}/pulls',
                                   params={'head': f'{owner}:{branch}', 'state': 'open',
                                           'base': base})
        response.raise_for_status()
        prs = response.json()
        if prs:
            existing = prs[0]
            if existing.get('head', {}).get('sha') != sha:
                raise ValueError('远端 PR head 与验收提交不一致')
            pr_number = _pr_number(existing, repo, allow_url_fallback=True)
            pr_url = _canonical_pr_url(existing, repo, pr_number)
            return {'publication_type': 'pull_request', 'repository_url': f'https://github.com/{repo}',
                    'pr_url': pr_url, 'pr_number': pr_number, 'repository': repo,
                    'commit': sha, 'branch': branch, 'published_branch': base}
        plan = run['plan']
        issue = run.get('source', {}).get('issue_number')
        body = f"{plan['summary']}\n\nRun: `{run['id']}` · Plan revision: {run['revision']}\n\n"
        body += 'Checks:\n' + '\n'.join(f"- {c.get('name', 'check')}: exit {c.get('exit', c.get('returncode', 'unknown'))}" for c in artifacts.get('checks', []))
        if issue:
            body += f'\n\nRefs #{int(issue)}'
        response = self.client.post(f'/repos/{repo}/pulls', json={
            'title': plan['title'][:200], 'body': body, 'head': branch,
            'base': base, 'draft': False})
        response.raise_for_status()
        created = response.json()
        # GitHub normally supplies ``number``. Keep the same strict URL-only
        # fallback as reconciliation for older/test doubles that do not.
        pr_number = _pr_number(created, repo, allow_url_fallback=True)
        pr_url = _canonical_pr_url(created, repo, pr_number)
        return {'publication_type': 'pull_request', 'repository_url': f'https://github.com/{repo}',
                    'pr_url': pr_url, 'pr_number': pr_number, 'repository': repo,
                'commit': sha, 'branch': branch, 'published_branch': base}

    def observe_merge(self, project: dict, run: dict) -> dict:
        """Independently verify the registered run's GitHub PR merge state.

        This method is deliberately read-only: the only remote operation is a
        GET against the canonical PR endpoint for the project's repository.
        """
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        repo = _repository(project['repository'])
        base_branch = project.get('github_base_branch') or project.get('base_branch')
        if not isinstance(base_branch, str) or not base_branch:
            raise ValueError('无效基线分支')
        artifacts = run.get('artifacts')
        if not isinstance(artifacts, dict):
            raise ValueError('运行缺少 GitHub 发布产物')
        branch = artifacts.get('branch')
        if not isinstance(branch, str) or not re.fullmatch(r'factory/[A-Za-z0-9_-]+', branch):
            raise ValueError('无效工厂交付分支')
        expected_head_sha = _sha(artifacts.get('commit'), '验收提交')

        stored_repository = artifacts.get('repository')
        if stored_repository is not None and not _same_repository(stored_repository, repo):
            raise ValueError('GitHub PR 仓库不匹配')
        stored_url = artifacts.get('pr_url')
        stored_number = artifacts.get('pr_number')
        if stored_number is not None:
            pr_number = _number(stored_number)
            if stored_url is not None and _pr_url_number(stored_url, repo) != pr_number:
                raise ValueError('GitHub PR 编号与 URL 不一致')
        elif stored_url is not None:
            pr_number = _pr_url_number(stored_url, repo)
        else:
            raise ValueError('运行缺少 GitHub PR 编号')

        response = self.client.get(f'/repos/{repo}/pulls/{pr_number}')
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('GitHub PR 响应无效')
        pr_url = _canonical_pr_url(payload, repo, pr_number, stored_url)

        # Scope is checked before merge state. In particular, a closed PR with
        # a changed head is not a harmless ordinary unmerged result.
        head = payload.get('head')
        base = payload.get('base')
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ValueError('GitHub PR 分支范围无效')
        head_repo = head.get('repo')
        base_repo = base.get('repo')
        if (not isinstance(head_repo, dict) or not _same_repository(head_repo.get('full_name'), repo)
                or not isinstance(base_repo, dict) or not _same_repository(base_repo.get('full_name'), repo)):
            raise ValueError('GitHub PR 仓库范围不匹配')
        if head.get('ref') != branch:
            raise ValueError('GitHub PR head 分支不匹配')
        if base.get('ref') != base_branch:
            raise ValueError('GitHub PR base 分支不匹配')
        actual_head_sha = _sha(head.get('sha'), 'PR head')
        if actual_head_sha != expected_head_sha:
            raise ValueError('GitHub PR head 与验收提交不一致')

        if payload.get('merged') is not True:
            return {'merged': False, 'reason': 'not_merged', 'pr_number': pr_number,
                    'pr_url': pr_url}

        merged_at = payload.get('merged_at')
        if not isinstance(merged_at, str) or not merged_at.strip():
            raise ValueError('GitHub PR 缺少合并时间')
        merge_sha = _sha(payload.get('merge_commit_sha'), '合并提交')
        return {'merged': True, 'repository': repo, 'pr_number': pr_number,
                'pr_url': pr_url, 'head_sha': actual_head_sha,
                'merge_commit_sha': merge_sha, 'base_branch': base_branch,
                'merged_at': merged_at}


def publish_failure_message(exc: Exception) -> str:
    """Actionable diagnostics without exposing request headers or token-bearing URLs."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = {401: 'GitHub 凭据无效或已过期，请更新凭据',
                  403: 'GitHub 拒绝访问，请检查仓库写入和 Pull Request 权限或访问限制',
                  404: 'GitHub 仓库不可见，请检查仓库名及凭据的仓库访问范围',
                  422: 'GitHub 无法创建 PR，请检查基础分支及是否存在可发布的差异'}.get(code, f'GitHub 服务返回 HTTP {code}，请稍后重试')
    elif isinstance(exc, (httpx.TimeoutException, subprocess.TimeoutExpired)):
        reason = '连接 GitHub 超时，请检查服务器网络后重试'
    elif isinstance(exc, httpx.TransportError):
        reason = '无法连接 GitHub，请检查服务器网络或代理后重试'
    elif isinstance(exc, ValueError) and str(exc).startswith(('尚未配置', '无效', '拒绝发布', '验收后', 'GitHub push', 'GitHub 仓库', 'GitHub 基础', 'GitHub 首次', 'GitHub 同名', '远端 PR')):
        reason = str(exc)
    else:
        reason = 'GitHub 发布失败，请检查发布配置及运行记录'
    return reason + '。本次验证结果保留，成果仍可查看和下载。'


def push_failure_message(stderr: str) -> str:
    """Classify git output locally; never return raw diagnostics or URLs."""
    text = (stderr or '').lower()
    if any(word in text for word in ('authentication failed', 'invalid username', 'could not read username', 'invalid credentials')):
        return 'GitHub push 失败：凭据无效或已过期，请更新发布凭据'
    if any(word in text for word in ('permission to', 'write access', '403', 'permission denied')):
        return 'GitHub push 失败：没有仓库写入权限，请检查凭据的仓库授权'
    if 'repository not found' in text:
        return 'GitHub push 失败：仓库不存在或当前凭据不可见，请检查仓库名与访问范围'
    if any(word in text for word in ('non-fast-forward', 'fetch first', 'stale info')):
        return 'GitHub push 失败：远端分支已经变化，请核对远端版本后重新验证，不要强制覆盖'
    if any(word in text for word in ('protected branch', 'repository rule', 'gh013', 'gh006', 'pre-receive hook declined')):
        return 'GitHub push 失败：仓库规则拒绝提交，请检查分支规则与提交要求'
    if any(word in text for word in ('could not resolve', 'failed to connect', 'timed out', 'ssl', 'connection reset', 'proxy')):
        return 'GitHub push 失败：网络、代理或证书连接异常，请检查服务器到 GitHub 的连接'
    return 'GitHub push 失败；检查凭据权限、网络或远端分支是否发生变化'
