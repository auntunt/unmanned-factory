/** Pure run-status → UI-state mapping for the conversational workspace.
 *  Kept side-effect-free so it can be unit-tested without rendering. */
export type Stage = 'intake' | 'build' | 'verify' | 'deliver'
export const STAGES: { key: Stage; label: string }[] = [
  { key: 'intake', label: '理解需求' }, { key: 'build', label: '制作' },
  { key: 'verify', label: '验收' }, { key: 'deliver', label: '交付' },
]
export type HeadState = 'active' | 'wait' | 'paused' | 'fail' | 'done'

const STAGE_OF: Record<string, Stage> = {
  requirement_analysis: 'intake', awaiting_spec_confirmation: 'intake', received: 'intake',
  planning: 'intake', needs_clarification: 'intake', awaiting_approval: 'intake',
  queued: 'build', running: 'build',
  verifying: 'verify',
  ready_for_review: 'deliver', publishing: 'deliver', published: 'deliver',
}
const HEAD_OF: Record<string, HeadState> = {
  requirement_analysis: 'active', received: 'active', planning: 'active', queued: 'active',
  running: 'active', verifying: 'active', publishing: 'active',
  needs_clarification: 'wait', awaiting_spec_confirmation: 'wait', awaiting_approval: 'wait',
  needs_human: 'paused', cancelled: 'paused',
  failed: 'fail', inspection_failed: 'fail', discarded: 'fail',
  ready_for_review: 'done', published: 'done',
}
export const HEAD_LABEL: Record<HeadState, string> = {
  active: '正在处理', wait: '等待你补充', paused: '已暂停', fail: '未能完成', done: '作品已完成',
}
export const TERMINAL_DONE = new Set(['ready_for_review', 'published'])
export const isTerminal = (s: string) => TERMINAL_DONE.has(s) || ['failed', 'cancelled', 'discarded', 'inspection_failed'].includes(s)

export function headState(status: string): HeadState { return HEAD_OF[status] ?? 'active' }
export function stageIndex(status: string): number {
  const stage = STAGE_OF[status]
  if (stage) return STAGES.findIndex(s => s.key === stage)
  return headState(status) === 'fail' ? 1 : 0
}

export type ComposerKind = 'clarify' | 'continue' | 'approve' | 'confirm' | 'followup' | 'revise'
export type Mode = { kind: ComposerKind; placeholder: string; hint: string; needsText: boolean; send?: string; approve?: string }

/** The single composer maps its send action to the right lifecycle call for the
 *  current run status; a typed adjustment at the approval gate re-opens planning. */
export function composerMode(status: string): Mode {
  switch (status) {
    case 'needs_clarification':
      return { kind: 'clarify', placeholder: '补充需要的信息…', hint: '补充后会继续当前任务。', needsText: true }
    case 'awaiting_spec_confirmation':
      return { kind: 'confirm', placeholder: '有要调整的地方可以直接说…', hint: '确认规格后开始制作。', needsText: false, approve: '确认规格，开始制作' }
    case 'awaiting_approval':
      return { kind: 'approve', placeholder: '想调整方案可以直接说…', hint: '批准后开始制作。', needsText: false, approve: '批准方案' }
    case 'needs_human':
      return { kind: 'continue', placeholder: '补充说明，或添加修正后的材料…', hint: '发送后会沿着当前任务继续。', needsText: true }
    case 'ready_for_review': case 'published':
      return { kind: 'revise', placeholder: '继续描述想调整的地方…', hint: '会保留已有成果并形成新版本。', needsText: true }
    case 'failed': case 'cancelled': case 'discarded': case 'inspection_failed':
      return { kind: 'revise', placeholder: '说明要怎么改，我重新制作…', hint: '会在同一项目上重新制作。', needsText: true }
    default:
      return { kind: 'followup', placeholder: '任务进行中，可以继续补充要求…', hint: '任务进行中，补充会在当前步骤后处理。', needsText: true }
  }
}
