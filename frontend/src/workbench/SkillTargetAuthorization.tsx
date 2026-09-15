import { useState } from 'react'
import type { Run } from '../workspace/types'
import { request } from '../workspace/api'
import { errorText, type PageProps } from './ui'

export default function SkillTargetAuthorization({ run, ...props }: PageProps & { run: Run }) {
  const [targets, setTargets] = useState(''); const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false); const [notice, setNotice] = useState('')
  const error = (run as Run & { error?: string }).error || ''
  if (props.user?.role !== 'admin' || run.status !== 'needs_human' || !error.includes('requires_authorization')) return null
  const submit = async () => {
    setBusy(true); setNotice('')
    try {
      await request(`/api/v4/runs/${run.id}/skill-target-authorization`, { method: 'POST', csrfToken: props.csrfToken,
        onUnauthorized: props.onUnauthorized, body: { revision: run.revision, targets: targets.split('\n').map(t => t.trim()).filter(Boolean) } })
      setNotice('本次运行的目标授权已记录。未完成规划的任务已重新入队；已有计划可点击继续工作。')
    } catch (e) { setNotice(errorText(e)) } finally { setBusy(false) }
  }
  return <section className="wb-card" aria-label="外部 skill 目标授权"><h3>确认本次运行的目标范围</h3>
    <p>授权仅适用于本次运行及其冻结的 skill 版本，不会授予其他运行。没有授权时平台拒绝执行进攻性 skill；离线分析请另选不含进攻能力的职能体。</p>
    <label>授权目标（每行一个域名、地址或资产标识）<textarea value={targets} onChange={e => setTargets(e.target.value)} maxLength={10000} /></label>
    <label><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我有权授权对这些目标执行本次任务</label>
    <button className="wb-button wb-button-primary" disabled={busy || !confirmed || !targets.trim()} onClick={() => void submit()}>确认目标授权</button>
    {notice && <p role="status">{notice}</p>}
  </section>
}
