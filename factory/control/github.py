"""Signed Issue intake and idempotent verified-branch delivery to GitHub."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import subprocess
from pathlib import Path

import httpx

REPOSITORY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')


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
        repo = project['repository']
        if not REPOSITORY.fullmatch(repo):
            raise ValueError('无效 GitHub 仓库名')
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
            raise ValueError('GitHub push 失败；检查凭据权限、网络或远端分支是否发生变化')
        owner = repo.split('/')[0]
        response = self.client.get(f'/repos/{repo}/pulls',
                                   params={'head': f'{owner}:{branch}', 'state': 'open',
                                           'base': project['base_branch']})
        response.raise_for_status()
        prs = response.json()
        if prs:
            if prs[0].get('head', {}).get('sha') != sha:
                raise ValueError('远端 PR head 与验收提交不一致')
            return {'pr_url': prs[0]['html_url'], 'commit': sha, 'branch': branch}
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
        return {'pr_url': response.json()['html_url'], 'commit': sha, 'branch': branch}
