import { useState } from 'react'
import { request } from '../workspace/api'
import { errorText, type PageProps } from './ui'

export default function NativePackImport({ onImported, ...props }: PageProps & { onImported: (id: string) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <details className="wb-card">
    <summary>恢复 webuddy 导出的职能包（v1/v2）</summary>
    <p>此入口只接收 webuddy 导出的职能包。其他来源的 skill ZIP 请使用上方“导入外部 skill 包”，先适配、验收，再人签启用。</p>
    <label>选择 webuddy 职能包 ZIP<input type="file" accept=".zip" disabled={busy} onChange={async e => {
      const input = e.currentTarget; const file = input.files?.[0]
      if (!file || busy) return
      setBusy(true); setError('')
      const body = new FormData(); body.append('file', file)
      try {
        const agent = await request<{ id: string }>('/api/v4/agent-packs/import', { method: 'POST', body, csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized })
        onImported(agent.id)
      } catch (cause) { setError(errorText(cause)) }
      finally { input.value = ''; setBusy(false) }
    }} /></label>
    {busy && <p role="status">正在恢复职能包…</p>}
    {error && <p role="alert">{error}</p>}
  </details>
}
