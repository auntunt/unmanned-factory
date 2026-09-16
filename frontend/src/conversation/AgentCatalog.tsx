import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, type PageProps } from '../workbench/ui'
import Icon, { CategoryBadge, packMark } from '../workbench/Icon'
import './conversation.css'

type Agent = { id: string; name: string; purpose?: string; builtin_pack?: string; active_version?: number }

/** 职能体目录：可浏览的岗位能力，展示用途、徽标与就绪状态。
 *  不是开始任务的必经步骤——用户默认直接在首页描述目标即可。 */
export default function AgentCatalog({ onUnauthorized }: PageProps) {
  const [agents, setAgents] = useState<Agent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const c = new AbortController()
    request<{ agents: Agent[] }>('/api/v4/agents', { onUnauthorized, signal: c.signal })
      .then(r => setAgents(r.agents || []))
      .catch(cause => { if (!c.signal.aborted) setError(errorText(cause)) })
    return () => c.abort()
  }, [onUnauthorized])
  return (
    <div className="cv-page">
      <h1>职能体</h1>
      <p className="cv-page-sub">这些是长期沉淀的专业能力。日常制作会沿用项目已配置的能力，你不必先选择。</p>
      {error && <div className="cv-error" role="alert"><span>{error}</span></div>}
      {agents === null && !error && <div className="cv-loading"><span className="cv-spinner" />正在读取…</div>}
      {agents && agents.length === 0 && <div className="cv-page-empty">还没有职能体。</div>}
      {agents && agents.length > 0 && <div className="cv-catalog">
        {agents.map(agent => (
          <article className="cv-agent-card" key={agent.id}>
            <header>
              <CategoryBadge avatar={Array.from(agent.name)[0]} mark={packMark(agent)} />
              <div><strong>{agent.name}</strong><small>v{agent.active_version ?? '—'}</small></div>
            </header>
            <p>{agent.purpose || '还没有用途说明。'}</p>
            <footer>
              <span className="cv-agent-ready"><Icon name="preview" width={15} height={15} /> {agent.active_version ? '已就绪' : '待完善'}</span>
              <Link className="cv-agent-dl" to={`/agents/${encodeURIComponent(agent.id)}?mode=maintain`}>维护方法 <Icon name="arrow" width={14} height={14} /></Link>
              {agent.builtin_pack && <a className="cv-agent-dl" title="下载平台原始模板" href={`/api/v4/builtin-packs/${encodeURIComponent(agent.builtin_pack)}/download`}><Icon name="download" width={14} height={14} /> 职能包</a>}
            </footer>
          </article>
        ))}
      </div>}
    </div>
  )
}
