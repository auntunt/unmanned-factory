import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Policy } from './v3-types'
import { errorText } from './ui'
import './project-autonomy.css'

export default function ProjectMode({ projectId, isAdmin, onUnauthorized }: { projectId: string; isAdmin: boolean; onUnauthorized: () => void }) {
  const [policy, setPolicy] = useState<Policy | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setPolicy(null); setError(null)
    void request<Policy>(`/api/v3/projects/${encodeURIComponent(projectId)}/policy`, { onUnauthorized, signal: controller.signal })
      .then((next) => { if (!controller.signal.aborted) setPolicy(next) })
      .catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [projectId, onUnauthorized])
  return <div className="pa-project-mode"><div>
    <strong>{policy ? policy.mode === 'autonomous' ? 'Auto · 自动执行' : '监督模式 · 等待计划批准' : error ? '执行模式暂不可用' : '正在读取执行模式…'}</strong>
    <small>{policy ? policy.mode === 'autonomous' ? '新需求在配置范围内自动推进，过程按版本留档。' : '新需求规划完成后，需要手动批准才会开始执行。' : error}</small>
  </div>{isAdmin && <Link className="wb-text-link" to={`/projects/${encodeURIComponent(projectId)}?tab=automation#project-autonomy`}>{policy?.mode === 'supervised' ? '改为 Auto →' : '执行策略 →'}</Link>}</div>
}
