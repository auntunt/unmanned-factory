/** Pure run-status → UI-state mapping for the conversational workspace.
 *  Kept side-effect-free so it can be unit-tested without rendering. */
export type Stage = 'intake' | 'build' | 'verify' | 'deliver'
export const STAGES: { key: Stage; label: string }[] = [
  { key: 'intake', label: '理解需求' }, { key: 'build', label: '制作' },
  { key: 'verify', label: '验收' }, { key: 'deliver', label: '交付' },
]
export type HeadState = 'active' | 'wait' | 'paused' | 'fail' | 'done' | 'unknown'

const STAGE_OF: Record<string, Stage> = {
  requirement_analysis: 'intake', awaiting_spec_confirmation: 'intake', received: 'intake',
  planning: 'intake', needs_clarification: 'intake', awaiting_approval: 'intake',
  queued: 'build', running: 'build',
  verifying: 'verify', inspection_completed: 'verify', inspection_failed: 'verify', interrupted: 'build',
  ready_for_review: 'deliver', publishing: 'deliver', published: 'deliver',
}
const HEAD_OF: Record<string, HeadState> = {
  requirement_analysis: 'active', received: 'active', planning: 'active', queued: 'active',
  running: 'active', verifying: 'active', publishing: 'active',
  needs_clarification: 'wait', awaiting_spec_confirmation: 'wait', awaiting_approval: 'wait',
  needs_human: 'paused', cancelled: 'paused', interrupted: 'paused',
  failed: 'fail', inspection_failed: 'fail', discarded: 'fail',
  ready_for_review: 'done', published: 'done', inspection_completed: 'done',
}
export const HEAD_LABEL: Record<HeadState, string> = {
  active: '正在处理', wait: '等待你补充', paused: '已暂停', fail: '未能完成', done: '作品已完成', unknown: '状态暂不支持',
}
export const TERMINAL_DONE = new Set(['ready_for_review', 'published'])
export const isTerminal = (s: string) => TERMINAL_DONE.has(s) || ['failed', 'cancelled', 'discarded', 'inspection_failed', 'inspection_completed', 'interrupted'].includes(s)

export function headState(status: string): HeadState { return HEAD_OF[status] ?? 'unknown' }
export function stageIndex(status: string): number {
  const stage = STAGE_OF[status]
  if (stage) return STAGES.findIndex(s => s.key === stage)
  return headState(status) === 'fail' ? 1 : 0
}

export type FollowUpBadge = 'pending' | 'applied' | 'expired' | 'none'

export interface FollowUpStatus {
  id: string
  content: string
  created_at: string
  applied: boolean
  expired?: boolean
}

/** Determine the badge for a follow-up message.
 *  Uses the message's own pending_id to look up its specific followup item
 *  status.  Falls back to global list only when pending_id is unavailable. */
export function followUpBadge(msg: { followup?: boolean; applied?: boolean; pending_id?: string }, followups: FollowUpStatus[]): FollowUpBadge {
  if (!msg.followup) return 'none'
  if (msg.applied) return 'applied'
  // Per-item lookup: find the specific followup by pending_id
  if (msg.pending_id && followups.length > 0) {
    const item = followups.find(f => f.id === msg.pending_id)
    if (item) {
      if (item.applied) return 'applied'
      if (item.expired) return 'expired'
      return 'pending'
    }
  }
  // Fallback when pending_id is not available: use global list
  if (followups.length === 0) return 'pending'
  const allApplied = followups.every(f => f.applied)
  if (allApplied) return 'applied'
  const allExpired = followups.every(f => f.expired || f.applied)
  if (allExpired) return 'expired'
  return 'pending'
}

export const FOLLOWUP_BADGE_LABEL: Record<FollowUpBadge, string> = {
  pending: '待应用',
  applied: '已并入后续执行',
  expired: '任务已结束未并入',
  none: '',
}

export type ComposerKind = 'clarify' | 'continue' | 'approve' | 'confirm' | 'followup' | 'revise' | 'readonly'
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
      return { kind: 'continue', placeholder: '补充说明，或添加修正后的材料…', hint: '已有执行现场会接续恢复；否则按当前配置重试，保留历史记录。', needsText: false, approve: '继续处理' }
    case 'ready_for_review': case 'published':
      return { kind: 'revise', placeholder: '继续描述想调整的地方…', hint: '在同一项目提交新任务，原任务与下载成果保留。', needsText: true }
    case 'failed': case 'cancelled': case 'discarded': case 'inspection_failed':
      return { kind: 'revise', placeholder: '说明要怎么改，我重新制作…', hint: '会在同一项目上重新制作。', needsText: true }
    case 'requirement_analysis': case 'received': case 'planning': case 'queued': case 'running': case 'verifying': case 'publishing':
      return { kind: 'followup', placeholder: '任务进行中，可以继续补充要求…', hint: '补充将在下一个安全节点自动并入任务。', needsText: true }
    default:
      return { kind: 'readonly', placeholder: '此状态仅供查看', hint: status === 'inspection_completed' ? '巡检已完成，仅记录诊断结果。' : status === 'interrupted' ? '任务已中断，请查看记录；恢复由后台状态决定。' : '当前状态暂不支持操作，请刷新或查看运行记录。', needsText: false }
  }
}
