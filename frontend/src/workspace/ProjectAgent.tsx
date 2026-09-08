import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import { request, WorkspaceApiError } from './api'
import {
  formatUnknown,
  graphEdgeIsRenderable,
  githubSourceUrl,
  markdownBundle,
  parseImportBundle,
  type AgentTab,
  type CodeGraph,
  type CodeIndexMeta,
  type CodeSearchResult,
  type ImportPreview,
  type KnowledgeEntry,
  type KnowledgeKind,
  type KnowledgeStatus,
  type ProjectAgentProfile,
} from './project-agent-types'
import './project-agent.css'

interface ProjectAgentProps {
  projectId: string | number
  csrfToken: string
  onUnauthorized: () => void
  repository?: string
}

const tabs: Array<{ id: AgentTab; label: string }> = [
  { id: 'profile', label: '档案' },
  { id: 'knowledge', label: '知识' },
  { id: 'code', label: '代码索引' },
  { id: 'import', label: 'TeamAI 交换' },
]

interface ProfileDraft {
  name: string
  mission: string
  architecture_summary: string
  constraints: string
}

const profileDefaults: ProfileDraft = {
  name: '', mission: '', architecture_summary: '', constraints: '',
}

const blankEntry = { kind: 'hypothesis' as KnowledgeKind, status: 'candidate' as KnowledgeStatus, title: '', content: '', paths: '', commit_sha: '' }

function apiError(cause: unknown): string {
  return cause instanceof WorkspaceApiError ? cause.detail : cause instanceof Error ? cause.message : '请求失败'
}

function warningText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object' && !Array.isArray(value) && typeof (value as Record<string, unknown>).message === 'string') return String((value as Record<string, unknown>).message)
  return formatUnknown(value)
}

function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === 'AbortError'
}

function dateText(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function StatusTag({ status }: { status: string }) {
  return <span className={`pa-tag pa-tag-${status}`}>{status === 'candidate' ? '候选' : status === 'active' ? '已采纳' : status === 'retired' ? '已退役' : status}</span>
}

function Provenance({ value }: { value: Record<string, unknown> }) {
  return <details className="pa-provenance"><summary>来源与审计</summary><pre>{formatUnknown(value)}</pre></details>
}

function ProfilePanel({ profile, draft, saving, error, onChange, onSave }: {
  profile: ProjectAgentProfile | null
  draft: ProfileDraft
  saving: boolean
  error: string | null
  onChange: (key: keyof ProfileDraft, value: string) => void
  onSave: (event: FormEvent) => void
}) {
  return <section className="pa-panel">
    <div className="pa-panel-heading"><div><h2>项目代理档案</h2><p>档案是项目级约束与目标，保存使用修订号 CAS。</p></div>{profile && <span className="pa-revision">修订 {profile.revision}</span>}</div>
    {error && <div className="pa-error" role="alert">{error}</div>}
    {!profile ? <div className="pa-loading">正在读取档案…</div> : <form onSubmit={onSave} className="pa-form">
      <label>名称<input maxLength={120} value={draft.name} onChange={(event) => onChange('name', event.target.value)} /></label>
      <label>使命<textarea maxLength={2000} rows={4} value={draft.mission} onChange={(event) => onChange('mission', event.target.value)} /></label>
      <label>架构摘要<textarea maxLength={4000} rows={6} value={draft.architecture_summary} onChange={(event) => onChange('architecture_summary', event.target.value)} /></label>
      <label>约束（每行一条，最多 20 条）<textarea rows={5} value={draft.constraints} onChange={(event) => onChange('constraints', event.target.value)} /></label>
      <div className="pa-actions"><button className="wf-button wf-button-primary" disabled={saving}>{saving ? '保存中…' : '保存档案'}</button></div>
    </form>}
  </section>
}

function EntryEditor({ initial, saving, onCancel, onSave }: { initial: typeof blankEntry; saving: boolean; onCancel?: () => void; onSave: (entry: typeof blankEntry) => void }) {
  const [draft, setDraft] = useState(initial)
  return <form className="pa-entry-editor" onSubmit={(event) => { event.preventDefault(); onSave(draft) }}>
    <div className="pa-form-grid"><label>类型<select value={draft.kind} onChange={(event) => setDraft((current) => ({ ...current, kind: event.target.value as KnowledgeKind }))}><option value="fact">事实</option><option value="decision">决策</option><option value="hypothesis">假设</option></select></label><label>状态<select value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as KnowledgeStatus }))}><option value="candidate">候选</option><option value="active">已采纳</option><option value="retired">已退役</option></select></label></div>
    <label>标题<input maxLength={200} required value={draft.title} onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))} /></label>
    <label>内容<textarea maxLength={8000} required rows={6} value={draft.content} onChange={(event) => setDraft((current) => ({ ...current, content: event.target.value }))} /></label>
    <label>相关路径（逗号分隔）<input value={draft.paths} onChange={(event) => setDraft((current) => ({ ...current, paths: event.target.value }))} placeholder="src/app.ts, docs/design.md" /></label>
    <label>提交 SHA（可选）<input value={draft.commit_sha} onChange={(event) => setDraft((current) => ({ ...current, commit_sha: event.target.value }))} placeholder="40 位十六进制 SHA" /></label>
    <div className="pa-actions"><button className="wf-button wf-button-primary" disabled={saving}>{saving ? '提交中…' : '保存条目'}</button>{onCancel && <button type="button" className="wf-button" onClick={onCancel}>取消</button>}</div>
  </form>
}

function KnowledgePanel({ projectId, csrfToken, onUnauthorized }: ProjectAgentProps) {
  const [entries, setEntries] = useState<KnowledgeEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editor, setEditor] = useState<{ entry?: KnowledgeEntry } | null>(null)
  const [saving, setSaving] = useState(false)
  const [history, setHistory] = useState<Record<string, unknown[]> | null>(null)
  const [historyKey, setHistoryKey] = useState<string | null>(null)
  const mutationController = useRef<AbortController | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setError(null)
    try {
      const response = await request<{ entries: KnowledgeEntry[] }>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/knowledge?include_retired=true`, { onUnauthorized, signal })
      if (!signal?.aborted) setEntries(response.entries)
    } catch (cause) { if (!isAbort(cause) && !signal?.aborted) setError(apiError(cause)) }
  }, [onUnauthorized, projectId])

  useEffect(() => { const controller = new AbortController(); setEntries(null); setEditor(null); setHistory(null); void load(controller.signal); return () => controller.abort() }, [load])
  useEffect(() => () => mutationController.current?.abort(), [])

  const saveEntry = async (draft: typeof blankEntry) => {
    mutationController.current?.abort(); const controller = new AbortController(); mutationController.current = controller; setSaving(true); setError(null)
    try {
      const body = { kind: draft.kind, status: draft.status, title: draft.title.trim(), content: draft.content, paths: draft.paths.split(',').map((path) => path.trim()).filter(Boolean), ...(draft.commit_sha.trim() ? { commit_sha: draft.commit_sha.trim() } : {}) }
      if (editor?.entry) {
        const next = await request<KnowledgeEntry>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/knowledge/${encodeURIComponent(editor.entry.key)}`, { method: 'PUT', csrfToken, onUnauthorized, signal: controller.signal, body: { ...body, expected_revision: editor.entry.revision } })
        if (!controller.signal.aborted) setEntries((current) => current?.map((item) => item.key === next.key ? next : item) ?? [next])
      } else {
        const next = await request<KnowledgeEntry>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/knowledge`, { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body })
        if (!controller.signal.aborted) setEntries((current) => current ? [next, ...current] : [next])
      }
      if (!controller.signal.aborted) setEditor(null)
    } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(apiError(cause)) } finally { if (mutationController.current === controller && !controller.signal.aborted) setSaving(false) }
  }

  const approve = async (entry: KnowledgeEntry) => {
    mutationController.current?.abort(); const controller = new AbortController(); mutationController.current = controller; setSaving(true); setError(null)
    try {
      const next = await request<KnowledgeEntry>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/knowledge/${encodeURIComponent(entry.key)}`, { method: 'PUT', csrfToken, onUnauthorized, signal: controller.signal, body: { kind: entry.kind, status: 'active', title: entry.title, content: entry.content, paths: entry.paths, ...(entry.commit_sha ? { commit_sha: entry.commit_sha } : {}), expected_revision: entry.revision } })
      if (!controller.signal.aborted) setEntries((current) => current?.map((item) => item.key === next.key ? next : item) ?? [next])
    } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(apiError(cause)) } finally { if (mutationController.current === controller && !controller.signal.aborted) setSaving(false) }
  }

  const showHistory = async (entry: KnowledgeEntry) => {
    setHistoryKey(entry.key); setError(null)
    try {
      const response = await request<{ versions: unknown[] }>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/knowledge/${encodeURIComponent(entry.key)}/versions`, { onUnauthorized })
      setHistory((current) => ({ ...(current ?? {}), [entry.key]: response.versions }))
    } catch (cause) { setError(apiError(cause)) } finally { setHistoryKey((current) => current === entry.key ? null : current) }
  }

  return <section className="pa-panel">
    <div className="pa-panel-heading"><div><h2>项目知识</h2><p>候选内容不会自动进入运行上下文；来源与版本始终可查。</p></div><button className="wf-button wf-button-primary" onClick={() => setEditor({})}>新增条目</button></div>
    {error && <div className="pa-error" role="alert">{error}</div>}
    {editor && <EntryEditor key={editor.entry ? `${editor.entry.key}:${editor.entry.revision}` : 'new'} initial={editor.entry ? { kind: editor.entry.kind, status: editor.entry.status, title: editor.entry.title, content: editor.entry.content, paths: editor.entry.paths.join(', '), commit_sha: editor.entry.commit_sha ?? '' } : blankEntry} saving={saving} onCancel={() => setEditor(null)} onSave={saveEntry} />}
    {entries === null ? <div className="pa-loading">正在读取知识条目…</div> : entries.length === 0 ? <div className="pa-empty">还没有知识条目。可以先添加候选假设或决策。</div> : <div className="pa-entry-list">{entries.map((entry) => <article className="pa-entry" key={entry.key}>
      <div className="pa-entry-title"><div><h3>{entry.title}</h3><div className="pa-tags"><StatusTag status={entry.status} /><span className="pa-tag">{entry.kind === 'fact' ? '事实' : entry.kind === 'decision' ? '决策' : '假设'}</span><span className="pa-muted">修订 {entry.revision} · {dateText(entry.created_at)}</span></div></div><div className="pa-entry-actions"><button className="wf-button" onClick={() => setEditor({ entry })}>编辑</button>{entry.status !== 'active' && <button className="wf-button" disabled={saving} onClick={() => void approve(entry)}>审核采纳</button>}<button className="wf-button" disabled={historyKey === entry.key} onClick={() => void showHistory(entry)}>{historyKey === entry.key ? '读取中…' : '历史'}</button></div></div>
      <pre className="pa-content">{entry.content}</pre>{entry.paths.length > 0 && <div className="pa-paths">路径：{entry.paths.join(' · ')}</div>}<Provenance value={entry.provenance} />
      {history?.[entry.key] && <div className="pa-history"><strong>版本历史</strong>{history[entry.key].map((version, index) => <pre key={index}>{formatUnknown(version)}</pre>)}</div>}
    </article>)}</div>}
  </section>
}

function GraphView({ graph, onSelect }: { graph: CodeGraph; onSelect: (node: string) => void }) {
  const width = Math.max(640, Math.min(1000, graph.nodes.length * 150))
  const columns = Math.max(1, Math.min(5, Math.ceil(Math.sqrt(graph.nodes.length || 1))))
  const rowHeight = 84
  const nodes = graph.nodes.map((node, index) => ({ node, x: 20 + (index % columns) * ((width - 50) / columns), y: 25 + Math.floor(index / columns) * rowHeight }))
  const positions = new Map(nodes.map(({ node, x, y }) => [node.id, { x, y }]))
  const nodeIds = new Set(graph.nodes.map((node) => node.id))
  const height = Math.max(180, (Math.ceil(nodes.length / columns) + 1) * rowHeight)
  return <div className="pa-graph-scroll"><svg className="pa-graph" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="代码关系图">{graph.edges.filter((edge) => graphEdgeIsRenderable(edge, nodeIds)).map((edge, index) => { const source = positions.get(edge.source); const target = positions.get(edge.target); if (!source || !target) return null; return <line key={`${edge.source}-${edge.target}-${index}`} x1={source.x + 55} y1={source.y + 19} x2={target.x + 55} y2={target.y + 19} className={`pa-edge pa-edge-${edge.resolution}`} /> })}{nodes.map(({ node, x, y }) => <g key={node.id} className="pa-graph-node" transform={`translate(${x},${y})`} role="button" tabIndex={0} aria-label={`查看 ${node.path} ${node.name}`} onClick={() => onSelect(node.id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') onSelect(node.id) }}><rect width="110" height="40" rx="6" /><text x="8" y="15" className="pa-node-name">{node.name.slice(0, 16)}</text><text x="8" y="30" className="pa-node-path">{node.path.slice(0, 19)}</text></g>)}</svg></div>
}

function CodePanel({ projectId, csrfToken, onUnauthorized, repository }: ProjectAgentProps) {
  const [meta, setMeta] = useState<CodeIndexMeta | null>(null)
  const [graph, setGraph] = useState<CodeGraph | null>(null)
  const [results, setResults] = useState<CodeSearchResult[]>([])
  const [searchResultSha, setSearchResultSha] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const graphControllerRef = useRef<AbortController | null>(null)
  const endpoint = `/api/v2/projects/${encodeURIComponent(String(projectId))}/code-index`

  const loadMeta = useCallback(async (signal?: AbortSignal) => {
    try { const next = await request<CodeIndexMeta>(endpoint, { onUnauthorized, signal }); if (!signal?.aborted) setMeta(next) } catch (cause) { if (!isAbort(cause) && !signal?.aborted) setError(apiError(cause)) }
  }, [endpoint, onUnauthorized])
  useEffect(() => { const controller = new AbortController(); setMeta(null); setGraph(null); setResults([]); setSearchResultSha(null); void loadMeta(controller.signal); return () => { controller.abort(); graphControllerRef.current?.abort(); controllerRef.current?.abort() } }, [loadMeta])

  const build = async () => {
    setBusy('build'); setError(null)
    try { setMeta(await request<CodeIndexMeta>(endpoint, { method: 'POST', csrfToken, onUnauthorized })); setGraph(null); setResults([]); setSearchResultSha(null) } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(apiError(cause)) } finally { setBusy(null) }
  }
  const search = async (event: FormEvent) => {
    event.preventDefault(); if (!query.trim()) { setResults([]); setSearchResultSha(null); return }
    controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller; setBusy('search'); setError(null)
    try { const response = await request<{ results: CodeSearchResult[]; commit_sha?: string | null; current_sha?: string | null; stale?: boolean; warnings?: string[] }>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/code-search?q=${encodeURIComponent(query.trim())}`, { onUnauthorized, signal: controller.signal }); if (!controller.signal.aborted) { setResults(response.results); setSearchResultSha(response.commit_sha ?? null); setMeta((current) => ({ ...(current ?? {}), commit_sha: response.commit_sha, current_sha: response.current_sha, stale: response.stale, warnings: response.warnings })) } } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted) setError(apiError(cause)) } finally { if (controllerRef.current === controller) setBusy(null) }
  }
  const selectNode = async (nodeId: string) => {
    graphControllerRef.current?.abort(); const controller = new AbortController(); graphControllerRef.current = controller; setBusy('graph'); setError(null)
    try { const next = await request<CodeGraph>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/code-graph?node=${encodeURIComponent(nodeId)}`, { onUnauthorized, signal: controller.signal }); if (!controller.signal.aborted) setGraph(next) } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted) setError(apiError(cause)) } finally { if (graphControllerRef.current === controller) setBusy(null) }
  }
  return <section className="pa-panel">
    <div className="pa-panel-heading"><div><h2>代码索引</h2><p>索引来自固定 Git 对象；关系标记为语法解析或启发式。</p></div><button className="wf-button wf-button-primary" disabled={busy !== null} onClick={() => void build()}>{busy === 'build' ? '构建中…' : '构建 / 刷新索引'}</button></div>
    {error && <div className="pa-error" role="alert">{error}</div>}
    {meta?.stale && <div className="pa-warning">索引已过期：当前基线 SHA 与索引 SHA 不同，请刷新索引。</div>}
    {meta?.indexed === false || !meta ? <div className="pa-empty">{meta ? '尚未建立代码索引。' : '正在读取索引状态…'}</div> : <div className="pa-index-meta"><span>索引 SHA：{meta.commit_sha ?? '—'}</span><span>当前 SHA：{meta.current_sha ?? '—'}</span><span>构建时间：{dateText(meta.indexed_at ?? undefined)}</span>{meta.warnings?.map((warning) => <span className="pa-warning" key={warning}>{warning}</span>)}</div>}
    <form className="pa-search" onSubmit={search}><label htmlFor="code-search">搜索代码节点</label><input id="code-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="文件名、符号或中文文档" /><button className="wf-button" disabled={busy !== null}>搜索</button></form>
    {results.length > 0 && <div className="pa-search-results"><strong>搜索结果</strong>{results.map((result) => { const sourceUrl = githubSourceUrl(repository, searchResultSha, result.path, result.line); return <div className="pa-search-result-row" key={result.node_id}><button className="pa-search-result" onClick={() => void selectNode(result.node_id)}><span>{result.name}</span><span>{result.path}:{result.line}–{result.end_line}</span><small>{result.resolution === 'heuristic' ? '启发式' : '语法'} · {result.snippet}</small></button>{sourceUrl && <a className="pa-source-link" href={sourceUrl} target="_blank" rel="noreferrer">查看该版本源码</a>}</div> })}</div>}
    {graph && <div className="pa-graph-section"><div className="pa-graph-legend"><span>节点：{graph.nodes.length}</span><span>边：{graph.edges.length}</span><span>图 SHA：{graph.commit_sha ?? '—'}</span>{graph.stale && <span className="pa-warning">图已过期</span>}{graph.truncated && <span className="pa-warning">图已截断</span>}<span>虚线 = 启发式</span></div><GraphView graph={graph} onSelect={(node) => void selectNode(node)} /></div>}
  </section>
}

function ImportPanel({ projectId, csrfToken, onUnauthorized, repository }: ProjectAgentProps) {
  const [json, setJson] = useState('')
  const [repositoryDraft, setRepositoryDraft] = useState(repository ?? '')
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [busy, setBusy] = useState<'preview' | 'apply' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const operationController = useRef<AbortController | null>(null)
  const fileReadSerial = useRef(0)
  const invalidatePreview = () => { operationController.current?.abort(); operationController.current = null; fileReadSerial.current += 1; setPreview(null); setSelected([]); setBusy(null) }
  useEffect(() => { setRepositoryDraft(repository ?? ''); invalidatePreview() }, [repository])
  useEffect(() => () => operationController.current?.abort(), [])

  const readFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    invalidatePreview()
    if (files.length > 20) { setError('一次最多选择 20 个 Markdown 文件。'); return }
    if (files.some((file) => file.size > 64000)) { setError('每个 Markdown 文件大小不能超过 64000 字节。'); return }
    const serial = fileReadSerial.current
    try { const bundle = markdownBundle(repositoryDraft, await Promise.all(files.map(async (file) => ({ name: file.name, content: await file.text() })))); if (serial !== fileReadSerial.current) return; setJson(JSON.stringify(bundle, null, 2)); setError(null) } catch (cause) { if (serial === fileReadSerial.current) setError(apiError(cause)) }
  }
  const makePreview = async (event: FormEvent) => {
    event.preventDefault(); setBusy('preview'); setError(null); setNotice(null)
    const controller = new AbortController(); operationController.current?.abort(); operationController.current = controller
    try { const bundle = parseImportBundle(json); const next = await request<ImportPreview>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/wiki-import/preview`, { method: 'POST', csrfToken, onUnauthorized, body: bundle, signal: controller.signal }); if (!controller.signal.aborted) { setPreview(next); setSelected(next.documents.map((document) => document.index)) } } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(apiError(cause)) } finally { if (operationController.current === controller && !controller.signal.aborted) setBusy(null) }
  }
  const apply = async () => {
    if (!preview || selected.length === 0) return
    setBusy('apply'); setError(null); setNotice(null)
    const controller = new AbortController(); operationController.current?.abort(); operationController.current = controller
    try { const result = await request<{ entries: KnowledgeEntry[]; duplicate: boolean }>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/wiki-import/apply`, { method: 'POST', csrfToken, onUnauthorized, body: { preview_id: preview.id, sha256: preview.sha256, indices: selected }, signal: controller.signal }); if (!controller.signal.aborted) { setNotice(result.duplicate ? '这次预览此前已应用，未重复创建条目。' : `已导入 ${result.entries.length} 条候选知识。`); setPreview(null); setSelected([]) } } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(apiError(cause)) } finally { if (operationController.current === controller && !controller.signal.aborted) setBusy(null) }
  }
  return <section className="pa-panel">
    <div className="pa-panel-heading"><div><h2>TeamAI 显式交换</h2><p>只处理你明确选择的 Markdown 数据；不会执行 hooks、MCP、资源注入或任意 URL。</p></div></div>
    {error && <div className="pa-error" role="alert">{error}</div>}{notice && <div className="pa-success" role="status">{notice}</div>}
    <form className="pa-form" onSubmit={makePreview}><label>repository（owner/name）<input required value={repositoryDraft} onChange={(event) => { invalidatePreview(); setRepositoryDraft(event.target.value) }} /></label><label>粘贴 JSON 交换包<textarea rows={11} value={json} onChange={(event) => { invalidatePreview(); setJson(event.target.value) }} placeholder={'{\n  "repository": "owner/name",\n  "documents": [{"path": "teamwiki/guide.md", "content": "..."}]\n}'} /></label><label>或选择 Markdown 文件<input type="file" accept=".md,text/markdown" multiple onChange={(event) => void readFiles(event)} /></label><div className="pa-actions"><button className="wf-button wf-button-primary" disabled={busy !== null || !json.trim()}>{busy === 'preview' ? '预览中…' : '生成导入预览'}</button></div></form>
    {preview && <div className="pa-preview"><div className="pa-panel-heading"><div><h3>导入预览（确认前不会写入）</h3><p>预览 SHA：{preview.sha256} · {preview.repository}</p></div><button className="wf-button wf-button-primary" disabled={busy !== null || selected.length === 0} onClick={() => void apply()}>{busy === 'apply' ? '应用中…' : `确认导入 ${selected.length} 项`}</button></div>{preview.warnings.map((warning, index) => <div className="pa-warning" key={`${warningText(warning)}-${index}`}>{warningText(warning)}</div>)}{preview.documents.map((document) => <label className="pa-preview-doc" key={document.index}><input type="checkbox" checked={selected.includes(document.index)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, document.index] : current.filter((index) => index !== document.index))} /><span><strong>{document.title}</strong><small>{document.path} · {document.sha256}</small><pre>{document.content}</pre></span></label>)}</div>}
  </section>
}

export default function ProjectAgent(props: ProjectAgentProps) {
  const [tab, setTab] = useState<AgentTab>('profile')
  const [profile, setProfile] = useState<ProjectAgentProfile | null>(null)
  const [draft, setDraft] = useState(profileDefaults)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const profileMutationController = useRef<AbortController | null>(null)
  const agentEndpoint = `/api/v2/projects/${encodeURIComponent(String(props.projectId))}/agent`

  useEffect(() => { const controller = new AbortController(); setProfile(null); setDraft(profileDefaults); setError(null); void request<ProjectAgentProfile>(agentEndpoint, { onUnauthorized: props.onUnauthorized, signal: controller.signal }).then((next) => { if (!controller.signal.aborted) { setProfile(next); setDraft({ name: next.name, mission: next.mission, architecture_summary: next.architecture_summary, constraints: next.constraints.join('\n') }) } }).catch((cause) => { if (!isAbort(cause) && !controller.signal.aborted) setError(apiError(cause)) }); return () => controller.abort() }, [agentEndpoint, props.onUnauthorized])
  useEffect(() => () => profileMutationController.current?.abort(), [])

  const saveProfile = async (event: FormEvent) => {
    event.preventDefault(); if (!profile) return
    const constraints = draft.constraints.split('\n').map((line) => line.trim()).filter(Boolean)
    if (constraints.length > 20) { setError('约束最多 20 条，请删减后再保存。'); return }
    if (constraints.some((line) => line.length > 300)) { setError('每条约束最多 300 个字符，请删减后再保存。'); return }
    profileMutationController.current?.abort(); const controller = new AbortController(); profileMutationController.current = controller; setSaving(true); setError(null)
    try { const next = await request<ProjectAgentProfile>(agentEndpoint, { method: 'PUT', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, signal: controller.signal, body: { expected_revision: profile.revision, name: draft.name, mission: draft.mission, architecture_summary: draft.architecture_summary, constraints } }); if (!controller.signal.aborted) { setProfile(next); setDraft({ name: next.name, mission: next.mission, architecture_summary: next.architecture_summary, constraints: next.constraints.join('\n') }) } } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted && cause instanceof WorkspaceApiError && cause.status === 409) setError('档案版本已变化，保存被拒绝。请重新读取后再编辑。'); else if (!isAbort(cause) && !controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(apiError(cause)) } finally { if (profileMutationController.current === controller && !controller.signal.aborted) setSaving(false) }
  }
  const panel = useMemo(() => { if (tab === 'profile') return <ProfilePanel profile={profile} draft={draft} saving={saving} error={error} onChange={(key, value) => setDraft((current) => ({ ...current, [key]: value }))} onSave={saveProfile} />; if (tab === 'knowledge') return <KnowledgePanel {...props} />; if (tab === 'code') return <CodePanel {...props} />; return <ImportPanel {...props} /> }, [draft, error, profile, props, saving, tab])
  return <section className="pa-root"><div className="pa-tabs" role="tablist" aria-label="项目代理面板">{tabs.map((item) => <button key={item.id} role="tab" aria-selected={tab === item.id} className={`pa-tab ${tab === item.id ? 'is-active' : ''}`} onClick={() => setTab(item.id)}>{item.label}</button>)}</div>{panel}</section>
}

export { ProjectAgent }
