import { useEffect, useState } from 'react'
import { request } from '../workspace/api'
import { ErrorNotice, errorText } from './ui'

type Config = { revision: number; webhook_configured: boolean; knowledge_enabled: boolean }
export default function OperationsSettings({ csrfToken, onUnauthorized }: { csrfToken: string; onUnauthorized: () => void }) {
  const [config, setConfig] = useState<Config | null>(null)
  const [webhook, setWebhook] = useState('')
  const [knowledge, setKnowledge] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void request<Config>('/api/v2/runtime/operations', { signal: controller.signal, onUnauthorized }).then(data => {
      if (!controller.signal.aborted) { setConfig(data); setKnowledge(data.knowledge_enabled) }
    }).catch(cause => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [onUnauthorized])
  async function save(clear = false) {
    if (!config) return
    setBusy(true); setError(null); setNotice('')
    try {
      const data = await request<Config>('/api/v2/runtime/operations', {
        method: 'PUT', csrfToken, onUnauthorized,
        body: { revision: config.revision, knowledge_enabled: knowledge, ...(clear ? { webhook: '' } : webhook ? { webhook } : {}) },
      })
      setConfig(data); setWebhook(''); setNotice('运维设置已保存。')
    } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <section className="wb-detail-card wb-runtime-section"><h2>无人值守运维</h2>
    <p>两项能力默认关闭。告警发送失败不影响任务，已验证事实可在项目页删除。</p>
    {config && <><label>飞书机器人 webhook<input type="password" autoComplete="off" value={webhook} onChange={e => setWebhook(e.target.value)} placeholder={config.webhook_configured ? '已配置；留空保留原地址' : '可选：飞书自定义机器人地址'} /></label>
      <label className="wb-checkbox"><input type="checkbox" checked={knowledge} onChange={e => setKnowledge(e.target.checked)} />回流并复用已验证的运维事实</label>
      <button className="wb-button wb-button-primary" disabled={busy} onClick={() => void save()}>保存运维设置</button>
      {config.webhook_configured && <button className="wb-button wb-button-secondary" disabled={busy} onClick={() => void save(true)}>关闭告警通知</button>}</>}
    {error && <ErrorNotice message={error} />}{notice && <p role="status">{notice}</p>}
  </section>
}
