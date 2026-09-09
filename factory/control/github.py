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

    def publish(self, project: dict, run: dict) -> dict:
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        repo = _repository(project['repository'])
        artifacts = run['artifacts']
        branch, sha = artifacts['branch'], artifacts['commit']
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('无效验收提交 SHA')
        if not re.fullmatch(r'factory/[A-Za-z0-9_-]+', branch):
            raise ValueError('拒绝发布非工厂交付分支')
        root = Path(artifacts['worktree'])
        current = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
            capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=root,
            capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        if current != sha or dirty:
            raise ValueError('验收后工作区已变化，拒绝发布；请重新验证')
        env = os.environ.copy()
        for key in list(env):
            if key.startswith('GIT_CONFIG_'):
                env.pop(key)
        header = base64.b64encode(f'x-access-token:{self.token}'.encode()).decode()
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='2',
                   GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {header}',
                   GIT_CONFIG_KEY_1='core.hooksPath', GIT_CONFIG_VALUE_1='/dev/null')
        result = subprocess.run(['git', 'push', f'https://github.com/{repo}.git',
                                 f'{sha}:refs/heads/{branch}'], cwd=root, env=env,
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            # HTTP authorization and low-level diagnostics stay out of user-visible logs.
            raise ValueError(push_failure_message(result.stderr))
        owner = repo.split('/')[0]
        response = self.client.get(f'/repos/{repo}/pulls',
                                   params={'head': f'{owner}:{branch}', 'state': 'open',
                                           'base': project['base_branch']})
        response.raise_for_status()
        prs = response.json()
        if prs:
            existing = prs[0]
            if existing.get('head', {}).get('sha') != sha:
                raise ValueError('远端 PR head 与验收提交不一致')
            pr_number = _pr_number(existing, repo, allow_url_fallback=True)
            pr_url = _canonical_pr_url(existing, repo, pr_number)
            return {'pr_url': pr_url, 'pr_number': pr_number, 'repository': repo,
                    'commit': sha, 'branch': branch}
        plan = run['plan']
        issue = run.get('source', {}).get('issue_number')
        body = f"{plan['summary']}\n\nRun: `{run['id']}` · Plan revision: {run['revision']}\n\n"
        body += 'Checks:\n' + '\n'.join(f"- {c.get('name', 'check')}: exit {c.get('exit', c.get('returncode', 'unknown'))}" for c in artifacts.get('checks', []))
        if issue:
            body += f'\n\nRefs #{int(issue)}'
        response = self.client.post(f'/repos/{repo}/pulls', json={
            'title': plan['title'][:200], 'body': body, 'head': branch,
            'base': project['base_branch'], 'draft': False})
        response.raise_for_status()
        created = response.json()
        # GitHub normally supplies ``number``. Keep the same strict URL-only
        # fallback as reconciliation for older/test doubles that do not.
        pr_number = _pr_number(created, repo, allow_url_fallback=True)
        pr_url = _canonical_pr_url(created, repo, pr_number)
        return {'pr_url': pr_url, 'pr_number': pr_number, 'repository': repo,
                'commit': sha, 'branch': branch}

    def observe_merge(self, project: dict, run: dict) -> dict:
        """Independently verify the registered run's GitHub PR merge state.

        This method is deliberately read-only: the only remote operation is a
        GET against the canonical PR endpoint for the project's repository.
        """
        if not self.token:
            raise ValueError('尚未配置 FACTORY_GITHUB_TOKEN')
        repo = _repository(project['repository'])
        base_branch = project.get('base_branch')
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
    elif isinstance(exc, ValueError) and str(exc).startswith(('尚未配置', '无效', '拒绝发布', '验收后', 'GitHub push', '远端 PR')):
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
