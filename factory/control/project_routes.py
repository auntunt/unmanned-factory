"""Project Agent routes mounted behind the existing authenticated gateway."""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from factory.control import codegraph
from factory.control.knowledge import KnowledgeStore
from factory.control.store import Conflict


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AgentUpdate(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=120)
    mission: str = Field(default='', max_length=2000)
    architecture_summary: str = Field(default='', max_length=4000)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeBody(StrictBody):
    kind: str = 'hypothesis'
    status: str = 'candidate'
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=8000)
    paths: list[str] = Field(default_factory=list, max_length=20)
    commit_sha: str | None = None


class KnowledgeUpdate(KnowledgeBody):
    expected_revision: int = Field(ge=1, strict=True)


class WikiDocument(StrictBody):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(min_length=1, max_length=16000)


class WikiBundle(StrictBody):
    repository: str = Field(min_length=3, max_length=250)
    documents: list[WikiDocument] = Field(min_length=1, max_length=20)


class WikiApply(StrictBody):
    preview_id: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    indices: list[StrictInt] = Field(min_length=1, max_length=20)


def router(store, service):
    api = APIRouter(prefix='/api/v2')
    memory = KnowledgeStore(store)
    index_slots = threading.BoundedSemaphore(2)
    index_lock = threading.Lock()
    indexing = set()

    def guarded(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def graph_data(pid):
        project = store.project(pid)
        sha = guarded(codegraph.baseline_sha, project)
        snapshot = codegraph.get_snapshot(store, pid, commit_sha=sha)
        return snapshot, sha

    def metadata(snapshot, sha):
        if snapshot is None:
            return {'indexed': False, 'current_sha': sha, 'warnings': ['尚未建立代码索引']}
        return {**{k: snapshot[k] for k in ('id', 'commit_sha', 'indexed_at', 'parser_version', 'stats', 'warnings')},
                'indexed': True, 'current_sha': sha, 'stale': snapshot['commit_sha'] != sha}

    @api.get('/projects/{pid}/agent')
    def get_agent(pid: str):
        return memory.agent(pid)

    @api.put('/projects/{pid}/agent')
    def update_agent(pid: str, body: AgentUpdate, request: Request):
        return guarded(memory.update_agent, pid, body.model_dump(exclude={'expected_revision'}),
                       body.expected_revision, request.state.user['username'])

    @api.get('/projects/{pid}/knowledge')
    def entries(pid: str, include_retired: bool = False):
        return {'entries': memory.entries(pid, include_retired=include_retired)}

    @api.post('/projects/{pid}/knowledge', status_code=201)
    def add_entry(pid: str, body: KnowledgeBody, request: Request):
        return guarded(memory.put_entry, pid, body.model_dump(), request.state.user['username'])

    @api.put('/projects/{pid}/knowledge/{key}')
    def edit_entry(pid: str, key: str, body: KnowledgeUpdate, request: Request):
        return guarded(memory.put_entry, pid, body.model_dump(exclude={'expected_revision'}),
                       request.state.user['username'], key=key, expected_revision=body.expected_revision)

    @api.get('/projects/{pid}/knowledge/{key}/versions')
    def entry_versions(pid: str, key: str):
        return {'versions': memory.versions(pid, key)}

    @api.post('/projects/{pid}/code-index')
    def index(pid: str):
        project = store.project(pid)
        with index_lock:
            if pid in indexing:
                raise Conflict('该项目正在建立索引，请稍后查看结果')
            if not index_slots.acquire(blocking=False):
                raise Conflict('索引任务已满，请稍后重试')
            indexing.add(pid)
        try:
            snapshot = guarded(codegraph.build_snapshot, project)
            stored = guarded(codegraph.save_snapshot, store, snapshot)
            return metadata(stored, guarded(codegraph.baseline_sha, project))
        finally:
            with index_lock:
                indexing.discard(pid)
                index_slots.release()

    @api.get('/projects/{pid}/code-index')
    def index_status(pid: str):
        snapshot, sha = graph_data(pid)
        return metadata(snapshot, sha)

    @api.get('/projects/{pid}/code-search')
    def search(pid: str, q: str = Query(default='', max_length=500), limit: int = Query(default=10, ge=1, le=30)):
        snapshot, sha = graph_data(pid)
        return {'results': guarded(codegraph.search_snapshot, snapshot, q, limit=limit) if snapshot else [],
                'commit_sha': snapshot['commit_sha'] if snapshot else None, 'current_sha': sha,
                'stale': bool(snapshot and snapshot['commit_sha'] != sha),
                'warnings': snapshot['warnings'] if snapshot else ['尚未建立代码索引']}

    @api.get('/projects/{pid}/code-graph')
    def graph(pid: str, node: str | None = Query(default=None, max_length=2000)):
        snapshot, sha = graph_data(pid)
        result = guarded(codegraph.graph_slice, snapshot, node_id=node) if snapshot else {'nodes': [], 'edges': [], 'truncated': False}
        return {**result, 'commit_sha': snapshot['commit_sha'] if snapshot else None, 'current_sha': sha,
                'stale': bool(snapshot and snapshot['commit_sha'] != sha)}

    @api.post('/projects/{pid}/wiki-import/preview')
    def preview(pid: str, body: WikiBundle, request: Request):
        return guarded(memory.preview_import, pid, body.model_dump(), request.state.user['username'])

    @api.post('/projects/{pid}/wiki-import/apply')
    def apply(pid: str, body: WikiApply, request: Request):
        return guarded(memory.apply_import, pid, body.preview_id, body.sha256, body.indices,
                       request.state.user['username'])

    @api.get('/projects/{pid}/knowledge-audit')
    def audit(pid: str, after: int = Query(default=0, ge=0)):
        rows = memory.audit(pid, after=after)
        return {'events': rows, 'cursor': rows[-1]['id'] if rows else after}

    @api.get('/runs/{rid}/context')
    def context(rid: str):
        return store.get(rid).get('context')

    @api.post('/runs/{rid}/sync-merge')
    def sync_merge(rid: str):
        try:
            return guarded(service.sync_merge, rid)
        except (Conflict, KeyError, HTTPException):
            raise
        except Exception:
            raise HTTPException(502, 'GitHub 合并状态核对失败，请稍后重试；没有写入未经确认的知识') from None

    return api
