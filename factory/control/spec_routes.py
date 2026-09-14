"""Authorized L0 reads and admin settings/bootstrap on the existing run queue."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from factory.control.spec_tree import BOOTSTRAP_REQUEST, SpecError, drift, git, safe_path, tree
from factory.control.store import Conflict, scrub
from factory.control.scope_declaration import recent


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    revision: int = Field(ge=1, strict=True)


class Generate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


def router(store, service, allowed_root):
    api = APIRouter(prefix='/api/v2/projects/{pid}/spec-tree')

    def project(pid, request):
        result = store.project(pid)
        service.governance.require_project(request.state.user['id'], pid)
        root = Path(result['workspace']).resolve()
        if not root.is_relative_to(allowed_root) or root == allowed_root:
            raise HTTPException(400, '项目工作区不在允许的目录内')
        return result

    def read(p, run_id=None):
        if not p.get('spec_tree_enabled'):
            return {'nodes': [], 'enabled': False, 'drift_count': 0}
        try:
            revision = 'HEAD'
            if run_id:
                run = store.get(run_id)
                if run['project_id'] != p['id']: raise HTTPException(404, '运行不属于该项目')
                revision = (run.get('artifacts') or {}).get('verification_commit') or (run.get('artifacts') or {}).get('commit')
                if not revision: raise Conflict('该运行尚无可读取的提交快照')
            sha = git(p['workspace'], 'rev-parse', '--verify', '--end-of-options', f'{revision}^{{commit}}').strip()
            nodes = tree(p['workspace'], sha)
            try:
                findings = drift(p['workspace'], sha)
                error = None
            except SpecError as exc:
                findings, error = {}, str(exc)
            for n in nodes:
                n['drift'] = findings.get(n['path'], {'level': 'none', 'commits': [], 'unverified': True,
                    'reasons': [error or '节点尚未归档，无法验证'], 'history': []})
                n['history'] = n['drift'].get('history', [])
                n['code_count'] = len(set(e['path'] for e in n['code']))
            return scrub({'nodes': nodes, 'enabled': True, 'commit': sha,
                'drift_count': sum(n['drift']['level'] != 'none' for n in nodes), 'error': error})
        except (SpecError, OSError) as exc:
            return {'nodes': [], 'enabled': True, 'drift_count': 0, 'error': str(exc)}

    @api.get('')
    def summary(pid: str, request: Request, run_id: str | None = Query(default=None, max_length=100)):
        data = read(project(pid, request), run_id)
        data['nodes'] = [{k:v for k,v in n.items() if k not in ('body','expanded','raw_source','frontmatter','history','code','related')} for n in data['nodes']]
        return data

    @api.get('/node')
    def detail(pid: str, request: Request, path: str = Query(max_length=1024), run_id: str | None = Query(default=None, max_length=100)):
        p = project(pid, request)
        try: safe_path(path)
        except SpecError as exc: raise HTTPException(400, str(exc)) from None
        if not path.startswith('.spec/') or not path.endswith('/spec.md'):
            raise HTTPException(400, '请选择 spec.md 节点')
        data = read(p, run_id)
        node = next((n for n in data['nodes'] if n['path'] == path), None)
        if node is None: raise HTTPException(404, '规格节点不存在')
        node['scope_declarations'] = recent(store, pid, node)
        return scrub(node)

    @api.put('/settings')
    def configure(pid: str, request: Request, body: Settings):
        project(pid, request)
        try:
            return store.update_project(pid, {'spec_tree_enabled': body.enabled}, body.revision, request.state.user['username'])
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @api.post('/generate', status_code=201)
    def generate(pid: str, request: Request, body: Generate):
        p = project(pid, request)
        if not p.get('spec_tree_enabled'): raise Conflict('请先启用规格树')
        run, created = store.create_run(pid, BOOTSTRAP_REQUEST,
            source={'type': 'web', 'actor': request.state.user['username'], 'actor_id': request.state.user['id'], 'operation': 'general', 'spec_bootstrap': True},
            delivery_id=f"spec-bootstrap:{request.state.user['id']}:{pid}:{body.idempotency_key}")
        if created:
            try: service.start_plan(run['id'])
            except Exception as exc:
                service._fail(run['id'], exc)
                raise
        return run
    return api
