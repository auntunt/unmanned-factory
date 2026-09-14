import RunStatusBadge from './RunStatusBadge'
import { runTitle, ListTime } from './presentation'
import { Link } from 'react-router-dom'
import type { Run, RunStatus } from '../workspace/types'
import { nextRunAction } from './run-guidance'
import type { OverviewData } from './v3-types'

export default function AttentionList({ items }: { items: NonNullable<OverviewData['attention']> }) {
  return <div className="pw-attention-list">{items.map((item) => {
    const run: Run = item.run_snapshot ?? { id: item.id, project_id: item.project_id ?? '', request: item.title ?? '',
      status: item.status as RunStatus, revision: 0, created_at: item.updated_at, updated_at: item.updated_at,
      artifacts: { needs_human: item.reason, billing_incomplete: item.billing_incomplete },
      triage: { questions: item.questions ?? [], reasons: [], decision: item.status === 'needs_clarification' ? 'needs_clarification' : 'human_approval', risk: 'low' } }
    return <article className="pw-attention-item" key={item.id}>
      <span className="pw-attention-indicator" aria-hidden="true">!</span>
      <div><span className="pw-attention-label"><RunStatusBadge run={run} />{item.project_name && <small> · {item.project_name}</small>}</span>
        <Link className="pw-attention-title" to={nextRunAction(run).href}>{runTitle(run)}</Link>
        <p><ListTime value={item.updated_at} /></p>
      </div>
      <Link className="wb-button wb-button-secondary" to={nextRunAction(run).href}>查看记录</Link>
    </article>
  })}</div>
}
