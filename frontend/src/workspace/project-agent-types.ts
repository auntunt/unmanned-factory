export type AgentTab = 'profile' | 'knowledge' | 'code' | 'import'

export interface ProjectAgentProfile {
  id: string
  project_id: string | number
  revision: number
  name: string
  mission: string
  architecture_summary: string
  constraints: string[]
  created_at: string
  updated_at: string
}

export type KnowledgeKind = 'fact' | 'decision' | 'hypothesis'
export type KnowledgeStatus = 'candidate' | 'active' | 'retired'

export interface KnowledgeEntry {
  id: string
  key: string
  project_id: string | number
  revision: number
  kind: KnowledgeKind
  status: KnowledgeStatus
  title: string
  content: string
  paths: string[]
  commit_sha?: string | null
  provenance: Record<string, unknown>
  actor?: string | null
  created_at: string
}

export interface CodeNode {
  id: string
  kind: 'file' | 'class' | 'function'
  path: string
  name: string
  line: number
  end_line: number
  language: string
  snippet: string
  resolution: 'syntax' | 'heuristic'
}

export interface CodeEdge {
  source: string
  target: string
  kind: 'contains' | 'imports' | 'calls'
  resolution: 'syntax' | 'heuristic'
  path: string
  line: number
}

export interface CodeIndexMeta {
  id?: string | number
  commit_sha?: string | null
  current_sha?: string | null
  indexed_at?: string | null
  parser_version?: string | null
  stats?: Record<string, unknown>
  warnings?: string[]
  stale?: boolean
  indexed?: boolean
}

export interface CodeSearchResult {
  node_id: string
  path: string
  name: string
  kind: string
  line: number
  end_line: number
  score: number
  snippet: string
  resolution: 'syntax' | 'heuristic'
}

export interface CodeGraph {
  nodes: CodeNode[]
  edges: CodeEdge[]
  truncated?: boolean
  commit_sha?: string | null
  current_sha?: string | null
  stale?: boolean
  warnings?: string[]
}

export interface ImportDocument {
  path: string
  title?: string
  content: string
}

export interface ImportBundle {
  repository: string
  documents: ImportDocument[]
}

export interface ImportPreview {
  id: string
  project_id: string | number
  sha256: string
  repository: string
  documents: Array<{ index: number; path: string; title: string; content: string; sha256: string }>
  warnings: unknown[]
  created_at: string
}

export interface RunContext {
  [key: string]: unknown
}

export function formatUnknown(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === undefined || value === null) return '—'
  try { return JSON.stringify(value, null, 2) } catch { return String(value) }
}

export function parseImportBundle(value: string): ImportBundle {
  let parsed: unknown
  try { parsed = JSON.parse(value) } catch { throw new Error('JSON 格式无效，请粘贴包含 repository 和 documents 的对象。') }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('导入内容必须是 JSON 对象。')
  const source = parsed as Record<string, unknown>
  if (typeof source.repository !== 'string' || !source.repository.trim()) throw new Error('必须填写 repository（owner/name）。')
  if (!Array.isArray(source.documents) || source.documents.length === 0) throw new Error('documents 必须是至少包含一项的数组。')
  if (source.documents.length > 20) throw new Error('一次最多导入 20 个文档。')
  let totalCharacters = 0
  const documents = source.documents.map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error(`第 ${index + 1} 个文档不是对象。`)
    const doc = item as Record<string, unknown>
    if (typeof doc.path !== 'string' || !doc.path.trim()) throw new Error(`第 ${index + 1} 个文档缺少 path。`)
    if (typeof doc.content !== 'string') throw new Error(`第 ${index + 1} 个文档缺少 content。`)
    if (doc.content.length > 16000) throw new Error(`第 ${index + 1} 个文档超过 16000 个字符。`)
    totalCharacters += doc.content.length
    return { path: doc.path, title: typeof doc.title === 'string' ? doc.title : undefined, content: doc.content }
  })
  if (totalCharacters > 160000) throw new Error('导入内容总计不能超过 160000 个字符。')
  return { repository: source.repository.trim(), documents }
}

export function markdownBundle(repository: string, files: Array<{ name: string; content: string }>): ImportBundle {
  const cleanRepository = repository.trim()
  if (!cleanRepository) throw new Error('请先填写 repository（owner/name）。')
  if (!files.length) throw new Error('请至少选择一个 Markdown 文件。')
  if (files.length > 20) throw new Error('一次最多选择 20 个 Markdown 文件。')
  if (files.some((file) => file.content.length > 16000)) throw new Error('每个 Markdown 文件最多 16000 个字符。')
  if (files.reduce((total, file) => total + file.content.length, 0) > 160000) throw new Error('导入内容总计不能超过 160000 个字符。')
  return {
    repository: cleanRepository,
    documents: files.map((file) => ({
      path: `teamwiki/${file.name.split(/[\\/]/).pop() || 'document.md'}`,
      content: file.content,
    })),
  }
}

export function graphEdgeIsRenderable(edge: CodeEdge, nodeIds: Set<string>): boolean {
  return nodeIds.has(edge.source) && nodeIds.has(edge.target)
}

export function githubSourceUrl(repository: string | undefined, commitSha: string | null | undefined, path: string, line: number): string | null {
  if (!repository || !/^[^/\s]+\/[^/\s]+$/.test(repository)) return null
  if (!commitSha || !/^[0-9a-fA-F]{40}$/.test(commitSha)) return null
  if (!Number.isInteger(line) || line < 1) return null
  if (!path || path.startsWith('/') || path.includes('\\')) return null
  const segments = path.split('/')
  if (segments.some((segment) => !segment || segment === '.' || segment === '..')) return null
  const encodedPath = segments.map((segment) => encodeURIComponent(segment)).join('/')
  const [owner, name] = repository.split('/')
  return `https://github.com/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/blob/${commitSha}/${encodedPath}?plain=1#L${line}`
}
