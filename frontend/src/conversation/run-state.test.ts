import { expect, it } from 'vitest'
import { composerMode, headState, stageIndex, isTerminal, STAGES } from './run-state'

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
