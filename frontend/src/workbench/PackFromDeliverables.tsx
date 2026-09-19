import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'

type File = { name: string; size?: number }
type Scope = '' | 'synthetic' | 'licensed'

/** This is the missing user entry into the existing executable-pack lifecycle.
 * Nothing is published by creating a draft; server-side selection checks remain authoritative. */
export default function PackFromDeliverables({ runId, files, csrfToken, onUnauthorized }: {
  runId: string; files: File[]; csrfToken: string; onUnauthorized: () => void
}) {
  const [selected, setSelected] = useState<Record<string, Scope>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [packId, setPackId] = useState('')
  const key = useRef(crypto.randomUUID())
  const change = (name: string, scope: Scope | null) => {
    setSelected(previous => {
      const next = { ...previous }
      if (scope === null) delete next[name]; else next[name] = scope
      return next
    })
    key.current = crypto.randomUUID()
  }
  const create = async () => {
    if (busy || !Object.keys(selected).length) return
    setBusy(true); setError('')
    try {
      const result = await request<{ id: string }>('/api/v4/capability-packs/drafts', {
        method: 'POST', csrfToken, onUnauthorized,
        body: { source_run_id: runId, operation_key: key.current,
          selections: Object.entries(selected).map(([name, scope]) => ({ name, ...(scope ? { material_scope: scope } : {}) })) },
      })
      setPackId(result.id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法创建工具包草稿') }
    finally { setBusy(false) }
  }
  return <details className="wb-card wb-form">
    <summary>转换为可复用工具</summary>
    <p>选择程序、方法与测试样例，创建职能包草稿。验证并发布后可挂靠到职能体；不会修改本次成果，也不会自动部署。</p>
    {packId ? <p role="status">工具包草稿已创建。<Link to={`/ability-center/packs/${encodeURIComponent(packId)}`}>查看草稿并运行验证</Link></p> : <>
      <p>请包含 webuddy-pack.json 工具契约。客户原始材料和本次生成的纪要不应选入通用包。</p>
      <ul className="pk-list">{files.map(file => <li key={file.name}>
        <label><input type="checkbox" checked={file.name in selected} disabled={busy}
          onChange={e => change(file.name, e.target.checked ? '' : null)} />{file.name}</label>
        {file.name in selected && <label>文件用途<select aria-label={`${file.name} 文件用途`} disabled={busy}
          value={selected[file.name]} onChange={e => change(file.name, e.target.value as Scope)}>
          <option value="">程序或方法</option><option value="synthetic">合成测试样例</option>
          <option value="licensed">已获授权的测试样例</option>
        </select></label>}
      </li>)}</ul>
      <button className="wb-button wb-button-primary" disabled={busy || !Object.keys(selected).length} onClick={() => void create()}>
        {busy ? '创建中…' : '创建工具包草稿'}</button>
    </>}
    {error && <p role="alert">{error}</p>}
  </details>
}
