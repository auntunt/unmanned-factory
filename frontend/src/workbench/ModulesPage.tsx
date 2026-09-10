import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText, type PageProps } from './ui'
import './modules.css'

const categories = { style: '界面风格', knowledge: '业务知识', workflow: '工作方法', delivery: '交付规范' }
type Category = keyof typeof categories
type Module = { id: string; version: number; name: string; category: Category; description: string; instructions: string }
type Selection = { revision: number; modules: Module[] }
const blank = { name: '', category: 'knowledge' as Category, description: '', instructions: '' }

export default function ModulesPage({ projectId, csrfToken, onUnauthorized, user }: PageProps & { projectId?: string | number }) {
  const [modules, setModules] = useState<Module[]>([])
  const [binding, setBinding] = useState<Selection | null>(null)
  const [selected, setSelected] = useState<Module[]>([])
  const [filter, setFilter] = useState<Category | 'all'>('all')
  const [search, setSearch] = useState('')
  const [editor, setEditor] = useState<typeof blank | null>(null)
  const [editing, setEditing] = useState<Module | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const dialogRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!editor) return
    const previous = document.activeElement as HTMLElement | null
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) setEditor(null)
      if (event.key !== 'Tab') return
      const elements = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, textarea, a[href]') ?? [])
      const first = elements[0], last = elements[elements.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', handle)
    return () => { document.removeEventListener('keydown', handle); if (!dialogRef.current) previous?.focus() }
  }, [Boolean(editor), busy])
  const admin = user?.role !== 'member'
  const base = `/api/v4/projects/${encodeURIComponent(String(projectId))}/modules`
  const load = useCallback(async (signal?: AbortSignal) => {
    const [catalog, selection] = await Promise.all([
      request<{ modules: Module[] }>('/api/v4/modules', { onUnauthorized, signal }),
      projectId ? request<Selection>(base, { onUnauthorized, signal }) : Promise.resolve(null),
    ])
    if (signal?.aborted) return
    setModules(catalog.modules); setBinding(selection); setSelected(selection?.modules ?? [])
  }, [base, projectId, onUnauthorized])
  useEffect(() => { const c = new AbortController(); setLoading(true); void load(c.signal).catch(e => { if (!c.signal.aborted) setError(errorText(e)) }).finally(() => { if (!c.signal.aborted) setLoading(false) }); return () => c.abort() }, [load])
  const mutate = async (operation: () => Promise<void>) => { setBusy(true); setError(''); setNotice(''); try { await operation() } catch(e) { setError(errorText(e)) } finally { setBusy(false) } }
  const toggle = (m: Module) => {
    setNotice('')
    setSelected(old => old.some(x => x.id === m.id) ? old.filter(x => x.id !== m.id) : [...old.filter(x => m.category !== 'style' || x.category !== 'style'), m])
  }
  const save = () => mutate(async () => {
    const value = await request<Selection>(base, { method: 'PUT', csrfToken, onUnauthorized, body: { expected_revision: binding?.revision, modules: selected.map(({id,version}) => ({id,version})) } })
    setBinding(value); setSelected(value.modules); setNotice('组合已保存。后续新任务使用这份组合，正在运行的任务保持原版本。')
  })
  const saveModule = () => mutate(async () => {
    if (!editor) return
    const saved = await request<Module>(editing ? `/api/v4/modules/${editing.id}` : '/api/v4/modules', { method: editing ? 'PUT' : 'POST', csrfToken, onUnauthorized, body: { ...editor, ...(editing ? { expected_revision: editing.version } : {}) } })
    setModules(old => editing ? old.map(m => m.id === saved.id ? saved : m) : [...old, saved]); setEditor(null); setEditing(null)
    setNotice('模块版本已保存。已搭配的项目保持原版本，可移除后重新添加以采用新版。')
  })
  const dirty = JSON.stringify(selected.map(m => [m.id,m.version])) !== JSON.stringify((binding?.modules ?? []).map(m => [m.id,m.version]))
  const visible = modules.filter(m => (filter === 'all' || m.category === filter) && `${m.name} ${m.description}`.includes(search))
  return <div className="wb-page mod-page">
    <PageHeader title={projectId ? '能力随任务组合' : '能力模块'} description="风格、知识和工作方法分别维护，按项目自由搭配。" actions={admin ? <button className="wb-button wb-button-primary" onClick={() => { setEditor({...blank}); setEditing(null) }}>＋ 新建模块</button> : undefined} />
    {error && <ErrorNotice message={error} />}{notice && <p className="wb-notice" role="status">{notice}</p>}
    {loading ? <p role="status">正在读取能力模块…</p> : <div className={`mod-layout ${projectId ? 'has-composition' : ''}`}>
      <section aria-label="模块库">
        <div className="mod-toolbar"><div className="mod-filters" aria-label="模块类别">{(['all', ...Object.keys(categories)] as (Category|'all')[]).map(c => <button key={c} aria-pressed={filter === c} onClick={() => setFilter(c)}>{c === 'all' ? '全部' : categories[c]}</button>)}</div><input aria-label="搜索模块" placeholder="搜索模块" value={search} onChange={e => setSearch(e.target.value)} /></div>
        <div className="mod-grid">{visible.map(m => {
          const chosen = selected.find(x => x.id === m.id)
          return <article className={`mod-card ${chosen ? 'is-selected' : ''}`} key={m.id}>
            <div className="mod-card-head"><span className="mod-symbol" aria-hidden="true">{({style:'◫',knowledge:'▤',workflow:'↗',delivery:'▱'})[m.category]}</span><h2>{m.name}</h2><small>v{m.version}</small></div>
            <p>{m.description || '按需搭配到项目，作为该领域的工作指导。'}</p>
            <details><summary>查看内容</summary><p className="mod-instructions">{m.instructions}</p></details>
            <footer><span>{categories[m.category]}</span>{projectId && admin ? <button className="mod-toggle" aria-pressed={Boolean(chosen)} disabled={busy || (!chosen && selected.length >= 12)} onClick={() => toggle(m)}>{chosen ? `已添加 v${chosen.version} ✓` : '添加 ＋'}</button> : admin ? <button className="wb-text-link" onClick={() => { setEditing(m); setEditor({name:m.name,category:m.category,description:m.description,instructions:m.instructions}) }}>编辑模块 →</button> : null}</footer>
          </article>
        })}</div>
        {!visible.length && <div className="wb-empty-state"><h2>还没有这类模块</h2><p>将团队已有的规范、知识或方法整理成模块，就能反复使用。</p></div>}
      </section>
      {projectId ? <aside className="mod-composition" aria-label="本项目的能力"><h2>本项目的能力</h2><p>已选择 {selected.length} 个模块</p><div className="mod-base"><strong>平台通用开发能力</strong><p>基础开发与工具权限由平台管理。模块提供工作指导，不改变授权。</p></div>
        {Object.entries(categories).map(([key,label]) => { const group=selected.filter(m => m.category===key); return group.length ? <section className="mod-selected-group" key={key}><h3>{label}</h3>{group.map(m => <div className="mod-selected-row" key={m.id}><span><strong>{m.name}</strong><small>v{m.version}</small></span>{admin && <button aria-label={`移除${m.name}`} disabled={busy} onClick={() => toggle(m)}>移除</button>}</div>)}</section> : null })}
        {!selected.length && <p className="mod-empty">不搭配模块也能正常开发。先选择需要的能力即可。</p>}
        <div className="mod-save"><p>每个项目选择一种界面风格；业务知识、工作方法与交付规范可叠加。保存只影响新任务。</p>{admin && <button className="wb-button wb-button-primary" disabled={!binding || busy || !dirty} onClick={() => void save()}>{busy ? '保存中…' : '保存组合'}</button>}{error && <button className="wb-text-link" disabled={busy} onClick={() => void mutate(async () => { await load(); setNotice('已重新读取，未保存的选择已重置。') })}>重新读取组合</button>}<Link to="/agents">管理原有职能体 →</Link></div>
      </aside> : <p className="mod-library-note">在项目的「能力与知识」页面搭配模块。已有职能体和上传的 Skill 继续保留，可与模块共同使用。</p>}
    </div>}
    {editor && <div className="mod-dialog-backdrop"><section ref={dialogRef} className="mod-dialog" role="dialog" aria-modal="true" aria-labelledby="module-editor-title"><form onSubmit={e => {e.preventDefault(); void saveModule()}}><h2 id="module-editor-title">{editing ? '更新模块版本' : '创建能力模块'}</h2><label>名称<input autoFocus required maxLength={120} value={editor.name} onChange={e=>setEditor({...editor,name:e.target.value})} /></label><label>类别<select value={editor.category} onChange={e=>setEditor({...editor,category:e.target.value as Category})}>{Object.entries(categories).map(([v,label])=><option key={v} value={v}>{label}</option>)}</select></label><label>适用范围<input maxLength={1000} value={editor.description} onChange={e=>setEditor({...editor,description:e.target.value})} /></label><label>工作指导或业务知识<textarea required rows={8} maxLength={16000} value={editor.instructions} onChange={e=>setEditor({...editor,instructions:e.target.value})} /></label><p>写明适用场景、具体方法与限制。请勿填入密钥。</p>{error && <ErrorNotice message={error} />}<footer><button type="button" className="wb-button wb-button-secondary" disabled={busy} onClick={()=>setEditor(null)}>取消</button><button className="wb-button wb-button-primary" disabled={busy}>保存版本</button></footer></form></section></div>}
  </div>
}
