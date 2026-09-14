import type { Run } from '../workspace/types'
import { runGuidance } from './run-guidance'
export default function RunStatusBadge({ run }: { run: Run }) {
  const g = runGuidance(run)
  const tone = g.kind === 'failure' || run.status === 'inspection_failed' ? 'danger' : ['paused','budget','billing','approval','requirements','recovery'].includes(g.kind) ? 'warning' : run.status === 'publishing' ? 'active' : g.kind === 'delivery' ? 'success' : ['running','planning','queued','verifying'].includes(run.status) ? 'active' : 'neutral'
  return <span data-run-status className={`wb-status wb-status-${tone}`}><i aria-hidden="true" />{g.label}</span>
}
