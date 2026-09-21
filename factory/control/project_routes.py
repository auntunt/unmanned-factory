"""Project Agent routes mounted behind the existing authenticated gateway."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from factory.control import code_intel, codegraph
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

    def shared_layer(pid):
        """The multi-language layer's own status for this project.

        Reported beside the legacy snapshot rather than merged into it: the two
        cover different languages and different code versions (the shared layer
        reads the working tree, the legacy snapshot a fixed commit), so one
        combined 'indexed: true' would hide which of them actually answered.
        """
        workspace = (store.project(pid) or {}).get('workspace')
        if not workspace or not Path(workspace).is_dir():
            return {'indexed': False, 'outcome': code_intel.NOT_INDEXED,
                    'reason': '项目没有可用的工作区目录'}
        status = code_intel.index_status(workspace)
        return {k: status.get(k) for k in
                ('indexed', 'outcome', 'reason', 'last_indexed', 'languages',
                 'file_count', 'node_count', 'edge_count', 'pending_changes',
                 'stale', 'reindex_recommended', 'backend')}

    def metadata(snapshot, sha, *, pid=None):
        legacy = ({'indexed': False, 'current_sha': sha, 'warnings': ['尚未建立代码索引']}
                  if snapshot is None else
                  {**{k: snapshot[k] for k in ('id', 'commit_sha', 'indexed_at',
                                               'parser_version', 'stats', 'warnings')},
                   'indexed': True, 'current_sha': sha,
                   'stale': snapshot['commit_sha'] != sha})
        # Which languages the legacy indexer reads at all. Without this, an
        # empty Java result reads as "no matches" instead of "never parsed".
        legacy['covers'] = ['python', 'javascript', 'typescript']
        if pid is None:
            return legacy
        return {**legacy, 'shared_layer': shared_layer(pid)}

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
            # One user-visible action builds both: the legacy snapshot (Python
            # and JS/TS, at a fixed commit) and the shared multi-language layer
            # (Java/C#/Go/... over the working tree). Leaving the second one to
            # a separate call would mean 信创 lookups silently fell back to an
            # indexer that cannot read the customer's language.
            workspace = project.get('workspace')
            if workspace and Path(workspace).is_dir():
                code_intel.ensure_indexed(workspace)
            return metadata(stored, guarded(codegraph.baseline_sha, project), pid=pid)
        finally:
            with index_lock:
                indexing.discard(pid)
                index_slots.release()

    @api.get('/projects/{pid}/code-index')
    def index_status(pid: str):
        snapshot, sha = graph_data(pid)
        return metadata(snapshot, sha, pid=pid)

    @api.get('/projects/{pid}/code-conditions')
    def code_conditions(pid: str):
        """Which languages this project contains and whether we could build them.

        The build tier is probed here, never declared in the capability table:
        an index that reads C# says nothing about whether a .NET SDK exists on
        this machine, and .NET Framework additionally needs Windows.
        """
        workspace = (store.project(pid) or {}).get('workspace')
        if not workspace or not Path(workspace).is_dir():
            raise HTTPException(409, '项目没有可用的工作区目录，无法探测语言与工具链')
        return code_intel.project_conditions(workspace)

    @api.get('/code-intel/capabilities')
    def code_intel_capabilities():
        """What the shared code-query layer claims, and on what evidence.

        Read by the plugin/package display, so a package never shows a green
        'supported' that covers indexing, relations and rebuilding at once.
        """
        return code_intel.capability_report()

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
