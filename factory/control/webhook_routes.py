"""Signed GitHub event ingestion; execution and merge authority remain in Service."""
import hashlib
import json
import re
from fastapi import APIRouter, HTTPException, Request
from factory.control.github import REPOSITORY, verify_signature
from factory.control.store import Conflict


def router(store, svc, secret):
    api = APIRouter()

    @api.post('/api/v2/github/webhook')
    async def webhook(request: Request):
        raw = await request.body()
        if not verify_signature(raw, request.headers.get('x-hub-signature-256', ''), secret):
            raise HTTPException(401, 'Webhook 签名无效')
        event = request.headers.get('x-github-event')
        if event not in ('issues', 'pull_request'):
            return {'ignored': True}
        delivery = request.headers.get('x-github-delivery', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', delivery):
            raise HTTPException(400, '缺少有效 delivery id')
        if event == 'pull_request':
            try:
                payload = json.loads(raw)
                if payload.get('action') != 'closed':
                    return {'ignored': True}
                pull = payload['pull_request']
                if pull.get('merged') is not True:
                    return {'ignored': True, 'reason': 'not_merged'}
                repo = payload['repository']['full_name']
                number = payload['number']
                if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) or type(number) is not int or number < 1:
                    raise ValueError('invalid pull request identity')
            except (ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(400, '无效 GitHub Pull Request 事件') from None
            project = next((p for p in store.projects() if p['repository'].casefold() == repo.casefold()), None)
            if not project:
                return {'ignored': True, 'reason': 'repository_not_registered'}
            matching = store.published_runs_for_pr(project['id'], number, repo)
            if not matching:
                return {'ignored': True, 'reason': 'run_not_found'}
            # The signed event is only a trigger. Fetch GitHub's current PR state
            # independently; never promote webhook-provided SHAs or summaries.
            from starlette.concurrency import run_in_threadpool
            try:
                results = [await run_in_threadpool(svc.sync_merge, run['id']) for run in matching]
            except (Conflict, KeyError):
                raise
            except Exception:
                raise HTTPException(502, 'GitHub 合并状态核对失败；可重发事件或在控制台重试') from None
            return {'results': results}
        try:
            payload = json.loads(raw)
            if payload.get('action') not in ('opened', 'edited', 'labeled', 'reopened'):
                return {'ignored': True}
            repo = payload['repository']['full_name']
            issue = payload['issue']
            number = int(issue['number'])
            text = str(issue.get('title', '')) + '\n\n' + str(issue.get('body') or '')
            labels = {label['name'] for label in issue.get('labels', [])}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, '无效 GitHub Issue 事件') from None
        if 'pull_request' in issue or issue.get('state', 'open') != 'open':
            return {'ignored': True}
        project = next((p for p in store.projects() if p['repository'].casefold() == repo.casefold()), None)
        if not project:
            return {'ignored': True, 'reason': 'repository_not_registered'}
        # Deduplicate semantic issue revision as well as GitHub delivery id. Label events
        # with no content change can enable a single fresh, explicitly opted-in analysis.
        semantic_id = hashlib.sha256(json.dumps([repo, number, issue.get('updated_at'), text,
                                                 'factory-ready' in labels]).encode()).hexdigest()
        run, created = store.create_run(project['id'], text[:50_000],
            source={'type': 'github', 'issue_number': number,
                    'url': f'https://github.com/{repo}/issues/{number}',
                    'trusted_label': 'factory-ready' in labels, 'delivery_id': delivery},
            delivery_id=delivery, semantic_id=semantic_id)
        if created:
            previous = run['source'].get('previous_run_id')
            if previous:
                try:
                    svc.cancel(previous, actor='issue-revision')
                except Conflict:
                    pass  # A verified/publishing result requires explicit human review.
            try:
                svc.start_plan(run['id'])
            except Exception as exc:
                svc._fail(run['id'], exc)
                raise
        return {'run_id': run['id'], 'duplicate': not created}

    return api
