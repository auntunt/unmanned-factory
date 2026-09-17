import { expect, it } from 'vitest'
import { composerMode, headState, stageIndex, isTerminal, STAGES, followUpBadge, FOLLOWUP_BADGE_LABEL, type FollowUpStatus } from './run-state'

it('maps each run status to the correct progress head', () => {
  expect(headState('running')).toBe('active')
  expect(headState('verifying')).toBe('active')
  expect(headState('needs_clarification')).toBe('wait')
  expect(headState('awaiting_approval')).toBe('wait')
  expect(headState('needs_human')).toBe('paused')
  expect(headState('failed')).toBe('fail')
  expect(headState('inspection_failed')).toBe('fail')
  expect(headState('ready_for_review')).toBe('done')
  expect(headState('published')).toBe('done')
})

it('advances the stage index through intake→build→verify→deliver', () => {
  expect(STAGES.map(s => s.key)).toEqual(['intake', 'build', 'verify', 'deliver'])
  expect(stageIndex('planning')).toBe(0)
  expect(stageIndex('running')).toBe(1)
  expect(stageIndex('verifying')).toBe(2)
  expect(stageIndex('ready_for_review')).toBe(3)
})

it('routes the single composer to the right lifecycle action per status', () => {
  expect(composerMode('needs_clarification').kind).toBe('clarify')
  expect(composerMode('awaiting_approval')).toMatchObject({ kind: 'approve', approve: '批准方案' })
  expect(composerMode('awaiting_spec_confirmation')).toMatchObject({ kind: 'confirm', needsText: false })
  expect(composerMode('needs_human').kind).toBe('continue')
  expect(composerMode('running').kind).toBe('followup')
  expect(composerMode('ready_for_review').kind).toBe('revise')
  expect(composerMode('failed').kind).toBe('revise')
})

it('treats only terminal statuses as terminal for polling', () => {
  expect(isTerminal('running')).toBe(false)
  expect(isTerminal('ready_for_review')).toBe(true)
  expect(isTerminal('published')).toBe(true)
  expect(isTerminal('failed')).toBe(true)
  expect(isTerminal('cancelled')).toBe(true)
})

it('followUpBadge returns pending when followups have unapplied items', () => {
  const followups: FollowUpStatus[] = [
    { id: 'a', content: '加搜索', created_at: '2026-01-01', applied: false },
  ]
  expect(followUpBadge({ followup: true, applied: false }, followups)).toBe('pending')
  expect(FOLLOWUP_BADGE_LABEL.pending).toBe('待应用')
})

it('followUpBadge returns applied when all followups are consumed', () => {
  const followups: FollowUpStatus[] = [
    { id: 'a', content: '加搜索', created_at: '2026-01-01', applied: true },
    { id: 'b', content: '改颜色', created_at: '2026-01-01', applied: true },
  ]
  expect(followUpBadge({ followup: true, applied: false }, followups)).toBe('applied')
  expect(FOLLOWUP_BADGE_LABEL.applied).toBe('已并入后续执行')
})

it('followUpBadge returns none for non-followup messages', () => {
  expect(followUpBadge({ followup: false }, [])).toBe('none')
  expect(followUpBadge({}, [])).toBe('none')
})

it('followUpBadge returns applied when message itself says applied', () => {
  expect(followUpBadge({ followup: true, applied: true }, [])).toBe('applied')
})

it('followUpBadge returns pending when followups list is empty but message is a followup', () => {
  expect(followUpBadge({ followup: true, applied: false }, [])).toBe('pending')
})

it('followUpBadge uses per-item pending_id to determine badge', () => {
  const followups: FollowUpStatus[] = [
    { id: 'a', content: '已并入', created_at: '2026-01-01', applied: true },
    { id: 'b', content: '仍待', created_at: '2026-01-01', applied: false },
  ]
  // Message A has pending_id 'a' -> its item is applied -> badge = applied
  expect(followUpBadge({ followup: true, applied: false, pending_id: 'a' }, followups)).toBe('applied')
  // Message B has pending_id 'b' -> its item is pending -> badge = pending
  expect(followUpBadge({ followup: true, applied: false, pending_id: 'b' }, followups)).toBe('pending')
})

it('followUpBadge shows expired for a specific item that expired', () => {
  const followups: FollowUpStatus[] = [
    { id: 'a', content: '已并入', created_at: '2026-01-01', applied: true },
    { id: 'b', content: '未并入', created_at: '2026-01-01', applied: false, expired: true },
  ]
  expect(followUpBadge({ followup: true, applied: false, pending_id: 'b' }, followups)).toBe('expired')
  expect(FOLLOWUP_BADGE_LABEL.expired).toBe('任务已结束未并入')
})

it('followUpBadge falls back to global list when pending_id is missing', () => {
  const followups: FollowUpStatus[] = [
    { id: 'a', content: '已并入', created_at: '2026-01-01', applied: true },
    { id: 'b', content: '仍待', created_at: '2026-01-01', applied: false },
  ]
  // No pending_id, not all applied -> pending
  expect(followUpBadge({ followup: true, applied: false }, followups)).toBe('pending')
})

it('followUpBadge expired fallback when all are expired or applied', () => {
  const followups: FollowUpStatus[] = [
    { id: 'a', content: '已并入', created_at: '2026-01-01', applied: true },
    { id: 'b', content: '未并入', created_at: '2026-01-01', applied: false, expired: true },
  ]
  // No pending_id, all either applied or expired -> expired
  expect(followUpBadge({ followup: true, applied: false }, followups)).toBe('expired')
})

it('followup composer hint mentions automatic merging at safe node', () => {
  const mode = composerMode('running')
  expect(mode.kind).toBe('followup')
  expect(mode.hint).toContain('安全节点')
})

it('distinguishes completed inspections, interruptions and unknown statuses from active work', () => {
  expect(headState('inspection_completed')).toBe('done')
  expect(stageIndex('inspection_completed')).toBe(2)
  expect(isTerminal('inspection_completed')).toBe(true)
  expect(composerMode('inspection_completed').kind).toBe('readonly')
  expect(headState('interrupted')).toBe('paused')
  expect(isTerminal('interrupted')).toBe(true)
  expect(composerMode('interrupted').kind).toBe('readonly')
  expect(headState('future_status')).toBe('unknown')
  expect(composerMode('future_status').kind).toBe('readonly')
})
