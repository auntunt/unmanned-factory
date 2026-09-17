import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { ENV_LABEL, packsBase, type PackBinding, type PackTask } from '../workbench/pack-types'

/** 日常调用交付：上传文件 → 调用已挂靠的职能包版本 → 下载成果与校验结果。
 *
 *  这是受控的文件处理任务，不是开发运行：没有阶段条、没有代码文件树、不建项目。
 *  版本在提交时冻结；失败也保留候选产物并明确标注「未通过验证」，不冒称成功。 */
export default function CapabilityPanel({ agentId, csrfToken, onUnauthorized }: PageProps & { agentId: string }) {
  const [bindings, setBindings] = useState<PackBinding[] | null>(null)
  const [task, setTask] = useState<PackTask | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const opKey = useRef<string | null>(null)
  const gen = useRef(0)

  useEffect(() => {
    const mine = (gen.current += 1)
    const controller = new AbortController()
    setBindings(null); setTask(null); setError(null)
    request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(agentId)}`,
      { onUnauthorized, signal: controller.signal })
      .then(value => { if (gen.current === mine) setBindings(value.bindings) })
      .catch(() => { if (gen.current === mine) setBindings([]) })
    return () => controller.abort()
  }, [agentId, onUnauthorized])

  const poll = useCallback(async (taskId: string, mine: number) => {
    const deadline = Date.now() + 180_000
    for (;;) {
      const current = await request<PackTask>(`${packsBase}/invocations/${encodeURIComponent(taskId)}`, { onUnauthorized })
      if (gen.current !== mine) return  // switched role/conversation: never touch the new page
      setTask(current)
      if (['succeeded', 'failed', 'cancelled'].includes(current.status) || Date.now() > deadline) return
      await new Promise(resolve => setTimeout(resolve, 1200))
    }
  }, [onUnauthorized])

  const run = async (binding: PackBinding, event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = ''
    if (!file || busy) return
    const mine = gen.current
    setBusy(true); setError(null); setTask(null)
    // A stable key per submission: a lost response on retry replays the same task
    // instead of converting the same file twice.
    if (!opKey.current) opKey.current = crypto.randomUUID()
    try {
      const body = new FormData()
      body.append('file', file); body.append('agent_id', agentId)
      body.append('pack_id', binding.pack_id); body.append('operation_key', opKey.current)
      const started = await request<PackTask>(`${packsBase}/invocations`, { method: 'POST', csrfToken, onUnauthorized, body })
      if (gen.current !== mine) return
      setTask(started)
      await poll(started.id, mine)
      opKey.current = null
    } catch (cause) { if (gen.current === mine) setError(errorText(cause)) } finally { setBusy(false) }
  }

  if (!bindings || bindings.length === 0) return null

  return <div className="cv-capability">
    {bindings.map(binding => <div key={binding.id} className="cv-capability-row">
      <div>
        <strong>{binding.pack_name}</strong>
        <span className="cv-capability-version"> v{binding.version}</span>
        {binding.upgrade_available && <Link className="cv-capability-link"
          to={`/ability-center/packs/${encodeURIComponent(binding.pack_id)}?tab=versions`}>可升级到 v{binding.latest_version}</Link>}
      </div>
      {binding.environment?.status === 'unavailable'
        ? <span className="cv-capability-warn">{ENV_LABEL.unavailable}：{binding.environment.missing?.join('、') || '未知依赖'}，
          请到 <Link className="cv-capability-link" to="/settings/runtime">模型与执行</Link> 定位后重试</span>
        : <label className="cv-attach" title={`使用 ${binding.pack_name} v${binding.version} 处理文件`}>
          <Icon name="delivery" width={16} height={16} /> {busy ? '处理中…' : '上传文件并转换'}
          <input type="file" disabled={busy} onChange={event => void run(binding, event)} />
        </label>}
    </div>)}

    {error && <div className="cv-error" role="alert"><span>{error}</span></div>}

    {task && <div className={`cv-result ${task.status === 'succeeded' ? 'is-ok' : task.status === 'failed' ? 'is-bad' : ''}`}>
      <div className="cv-result-head">
        <strong>{task.status === 'succeeded' ? '转换完成' : task.status === 'failed' ? '未通过校验' : '处理中…'}</strong>
        <span className="cv-capability-version">所用能力版本 v{task.snapshot.version}</span>
      </div>
      {task.status === 'failed' && <p className="cv-result-reason">{task.error || '执行失败'}
        {task.error_code && <code> · {task.error_code}</code>}</p>}
      {task.result && Object.keys(task.result).length > 0 && <p className="cv-result-reason">
        {Object.entries(task.result).map(([key, value]) => `${key}：${String(value)}`).join(' · ')}</p>}
      {task.outputs.map(output => <a key={output.id} className="cv-filechip" style={{ textDecoration: 'none' }}
        href={`${packsBase}/artifacts/${encodeURIComponent(output.id)}/download`}>
        <Icon name="download" width={13} height={13} /><span>{output.name}</span>
        {output.validation_status !== 'passed' && <span className="cv-capability-warn">未通过验证</span>}
      </a>)}
      {task.status === 'failed' && task.outputs.length === 0 && <p className="cv-result-reason">没有产生可下载的结果文件。</p>}
    </div>}
  </div>
}
