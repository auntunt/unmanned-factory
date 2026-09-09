import type { Run } from '../workspace/types'

export const RUN_VIEWS = ['requirements', 'plan', 'execution', 'verification', 'delivery'] as const
export type RunView = typeof RUN_VIEWS[number]
export type RunStage = 'intake' | 'plan' | 'build' | 'verify' | 'deliver'
export type RunGuidanceKind = 'requirements' | 'approval' | 'budget' | 'billing' | 'failure' | 'recovery' | 'paused' | 'progress' | 'delivery'

export interface RunGuidance {
  kind: RunGuidanceKind
  label: string
  summary: string
  detail?: string
  view: RunView
  stage: RunStage
  primaryLabel: string
  primaryHref: string
  rawEvidence?: string
}

export interface FrozenRunPolicy {
  revision: number
  mode: 'supervised' | 'autonomous'
}

export interface RunEvidence {
  requirements: boolean
  plan: boolean
  execution: boolean
  checks: 'none' | 'passed' | 'failed' | 'recorded'
  delivery: boolean
}

const RECOVERY_PATTERN = /recover|recovery|resume|restart|interrupted|crash|恢复|重启|中断/i

function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) }
function records(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.filter(record) : [] }
function textArtifact(run: Run, key: string): string | undefined {
  const value = run.artifacts?.[key]
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}
export function executionFailure(run: Run): string | undefined {
  const tasks = records(run.tasks).length ? records(run.tasks) : records(run.artifacts?.tasks)
  for (const task of tasks) {
    if (task.status !== 'failed') continue
    const attempts = records(task.attempts)
    const latest = attempts[attempts.length - 1]
    const error = latest?.error ?? task.error
    if (typeof error === 'string' && error.trim()) return error.trim()
  }
  return undefined
}

export function frozenPolicy(run: Run): FrozenRunPolicy | null {
  const policy = (run as Run & { policy?: unknown }).policy
  if (!record(policy) || (policy.mode !== 'supervised' && policy.mode !== 'autonomous') || typeof policy.revision !== 'number') return null
  return { mode: policy.mode, revision: policy.revision }
}

export function canGenerateNextPlan(run: Run, canActOnRun: boolean): boolean {
  const guidance = runGuidance(run)
  return canActOnRun && ['needs_clarification', 'awaiting_approval', 'needs_human'].includes(run.status) && guidance.kind !== 'budget' && guidance.kind !== 'billing'
}

function runError(run: Run): string | undefined {
  const value = (run as Run & { error?: unknown }).error
  if (typeof value === 'string' && value.trim()) return value.trim()
  return textArtifact(run, 'error') ?? textArtifact(run, 'failure_reason')
}
function requirements(run: Run): string[] { return [...new Set([...(run.triage?.questions ?? []), ...(run.plan?.questions ?? [])].filter((question) => typeof question === 'string' && question.trim()))] }
function href(run: Run, view: RunView): string { return `/runs/${encodeURIComponent(String(run.id))}?view=${view}` }
function result(run: Run, values: Omit<RunGuidance, 'primaryHref'>): RunGuidance { return { ...values, primaryHref: href(run, values.view) } }

export function checkPassed(check: Record<string, unknown>): boolean {
  if (check.timeout === true || check.cancelled === true || typeof check.exit === 'number' && check.exit !== 0) return false
  const outcome = typeof check.outcome === 'string' ? check.outcome : typeof check.status === 'string' ? check.status : undefined
  return outcome !== undefined ? outcome === 'passed' || outcome === 'pass' : check.exit === 0
}

/** Reads only persisted delivery evidence; it never infers a completed stage from a later status. */
export function runEvidence(run: Run): RunEvidence {
  const progress = (run as Run & { progress?: RunEvidence }).progress
  if (progress) return progress
  const tasks = records(run.tasks).length ? records(run.tasks) : records(run.artifacts?.tasks)
  const attempts = tasks.flatMap((task) => records(task.attempts))
  const taskChecks = tasks.flatMap((task) => { const attempts = records(task.attempts); return attempts.length ? records(attempts[attempts.length - 1]?.checks) : records(task.checks) })
  const checks = records(run.artifacts?.checks).length ? records(run.artifacts?.checks) : taskChecks
  const hasFailed = checks.some((check) => check.outcome === 'failed' || check.outcome === 'fail' || check.status === 'failed' || check.timeout === true || typeof check.exit === 'number' && check.exit !== 0)
  const hasPassed = checks.length > 0 && checks.every(checkPassed)
  return {
    requirements: Boolean(run.request.trim()),
    plan: Boolean(run.plan),
    execution: attempts.length > 0 || tasks.some((task) => ['running', 'completed', 'verified', 'failed'].includes(String(task.status))),
    checks: !checks.length ? 'none' : hasFailed ? 'failed' : hasPassed ? 'passed' : 'recorded',
    delivery: typeof run.artifacts?.commit === 'string' && Boolean(run.artifacts.commit.trim()) || typeof run.artifacts?.pr_url === 'string' && Boolean(run.artifacts.pr_url.trim()),
  }
}

/** Converts persisted run state into an explicit, non-speculative next step. */
export function runGuidance(run: Run): RunGuidance {
  const questions = requirements(run)
  const stopReason = runError(run) ?? executionFailure(run) ?? textArtifact(run, 'needs_human')
  const failure = runError(run) ?? stopReason
  if (run.status === 'needs_human' && /cost|budget|quota|费用|预算|额度/i.test(stopReason || '')) return result(run, { kind: 'paused', label: '可以继续原任务', summary: '旧计费限制已停用，费用和额度统一由中转站管理。继续执行原计划即可。', view: 'execution', stage: 'build', primaryLabel: '回答并继续执行' })

  // Terminal runs retain their historical billing/budget artifacts without being presented as pending work.
  if (run.status === 'published') return result(run, { kind: 'delivery', label: '交付已发布', summary: '交付已发布；可查看提交和检查记录。', view: 'delivery', stage: 'deliver', primaryLabel: '查看交付证据' })
  if (run.status === 'discarded') return result(run, { kind: 'progress', label: '任务已废弃', summary: '旧任务已退出当前待办；计划和执行证据仍保留。', view: 'execution', stage: 'build', primaryLabel: '查看历史记录' })
  if (run.status === 'cancelled') return result(run, { kind: 'progress', label: '运行已取消', summary: '运行已主动结束；已产生的记录和证据仍可查看。', view: 'execution', stage: 'build', primaryLabel: '查看已记录现场' })
  if (run.status === 'ready_for_review' || run.status === 'publishing') return result(run, { kind: 'delivery', label: run.status === 'publishing' ? '正在发布交付' : '已验证，可查看成果', summary: run.status === 'publishing' ? '检查结果已记录，正在发布交付产物。' : '验证已通过。查看或下载本次成果，也可选择推送到 GitHub。', view: 'delivery', stage: 'deliver', primaryLabel: '查看和下载成果' })

  if (run.status === 'needs_human' && stopReason?.startsWith('out-of-scope changes:')) return result(run, { kind: 'paused', label: '修改超出当前任务范围', summary: '模型修改了当前任务未列入计划的文件，系统已暂停。在执行页回答后，助手会结合失败证据修正草稿，按原计划继续。', detail: stopReason, view: 'execution', stage: 'build', primaryLabel: '回答并继续执行', rawEvidence: stopReason })
  if (run.status === 'needs_human' && stopReason?.startsWith('worker changed test/check infrastructure:')) return result(run, { kind: 'paused', label: '测试配置改动需要处理', summary: '执行助手改动了受保护的测试配置。回答后将继续修正当前草稿，保留原计划与已完成任务。', detail: stopReason, view: 'execution', stage: 'build', primaryLabel: '回答并继续执行', rawEvidence: stopReason })
  if (run.status === 'needs_clarification' || (run.status === 'needs_human' && questions.length > 0) || (run.status === 'awaiting_approval' && questions.length > 0)) {
    const count = questions.length
    return result(run, { kind: 'requirements', label: count ? '需要补充需求' : '需要补充运行边界', summary: count ? `请回答 ${count} 个已记录问题，系统会据此重新规划。` : '当前没有可展示的具体问题；请补充目标、范围或验收标准后重新规划。', detail: questions[0], view: 'requirements', stage: 'intake', primaryLabel: '查看需求与补充', rawEvidence: stopReason })
  }
  if (run.status === 'awaiting_approval') {
    const policy = frozenPolicy(run)
    if (policy?.mode === 'autonomous') {
      const reasons = (run.triage?.reasons ?? []).filter((reason) => typeof reason === 'string' && reason.trim())
      return result(run, { kind: 'approval', label: '自动策略未覆盖本次计划', summary: reasons.length ? reasons.join('；') : `冻结的自主策略 v${policy.revision} 没有授权本次计划，系统没有自动开始。`, detail: reasons[0], view: 'plan', stage: 'plan', primaryLabel: '查看计划与判断依据', rawEvidence: stopReason })
    }
    return result(run, { kind: 'approval', label: '监督模式等待批准', summary: policy ? `本次运行冻结为监督模式 v${policy.revision}；批准当前版本后才能进入执行。` : '本次运行未返回策略冻结快照；批准当前版本后才能进入执行。', view: 'plan', stage: 'plan', primaryLabel: '查看计划并批准', rawEvidence: stopReason })
  }
  if (run.status === 'failed') return result(run, { kind: 'failure', label: '运行失败', summary: failure ? '运行记录了失败原因。请先查看失败和检查证据，再决定是否创建重试。' : '本次执行未形成可继续的结果。请先查看失败和检查证据，再决定是否创建重试。', detail: failure, view: 'verification', stage: 'verify', primaryLabel: '查看失败证据', rawEvidence: failure })
  if (run.status === 'needs_human' && stopReason && RECOVERY_PATTERN.test(stopReason)) return result(run, { kind: 'recovery', label: '恢复前暂停', summary: '运行在恢复现场前暂停，系统没有把未知写入自动重放。请查看已记录原因后再继续。', detail: stopReason, view: 'execution', stage: 'build', primaryLabel: '查看恢复现场', rawEvidence: stopReason })
  if (run.status === 'needs_human') return result(run, { kind: 'paused', label: '运行已暂停', summary: '运行没有记录可继续的自动操作；请查看原始停止原因和执行证据，按原因处理后再继续。', detail: stopReason, view: 'execution', stage: 'build', primaryLabel: '回答并继续执行', rawEvidence: stopReason })

  const values: Record<string, Pick<RunGuidance, 'label' | 'summary' | 'view' | 'stage' | 'primaryLabel'>> = {
    received: { label: '需求已接收', summary: '系统将分析目标和执行边界。', view: 'requirements', stage: 'intake', primaryLabel: '查看需求' },
    planning: { label: '正在规划', summary: '正在形成任务分工、依赖关系和验收方式。', view: 'plan', stage: 'plan', primaryLabel: '查看计划' },
    queued: { label: '等待执行资源', summary: '计划已进入队列，尚未开始模型调用。', view: 'execution', stage: 'build', primaryLabel: '查看执行队列' },
    running: { label: '正在执行', summary: '正在执行任务；模型尝试和修复记录会持续写入。', view: 'execution', stage: 'build', primaryLabel: '查看执行情况' },
    verifying: { label: '正在验证', summary: '正在运行配置的检查，结果会写入检查证据。', view: 'verification', stage: 'verify', primaryLabel: '查看验证情况' },
  }
  const fallback = values[run.status] ?? { label: '运行状态已记录', summary: '请查看运行现场中的已记录证据。', view: 'execution' as RunView, stage: 'build' as RunStage, primaryLabel: '查看运行现场' }
  return result(run, { kind: 'progress', ...fallback })
}

export function runView(value: string | null, fallback: RunView = 'requirements'): RunView { return RUN_VIEWS.includes(value as RunView) ? value as RunView : fallback }

export function nextRunAction(run: Run): { label: string; href: string } {
  const guidance = runGuidance(run)
  if (run.status === 'needs_human') return { label: '回答并继续', href: `/runs/${encodeURIComponent(String(run.id))}?view=execution#run-recovery` }
  if (run.status === 'awaiting_approval') return { label: '确认计划并开始执行', href: `/runs/${encodeURIComponent(String(run.id))}?view=plan` }
  if (['ready_for_review', 'published'].includes(run.status)) return { label: '查看和下载成果', href: `/runs/${encodeURIComponent(String(run.id))}?view=delivery#deliverables-title` }
  return { label: guidance.primaryLabel, href: guidance.primaryHref }
}
