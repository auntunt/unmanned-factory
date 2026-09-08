import type { ReactNode } from 'react'

import type { User } from '../workspace/types'

export interface PageProps {
  csrfToken: string
  onUnauthorized: () => void
}

export interface WorkbenchProps extends PageProps {
  user: User
  onLogout: () => void
}

export function PageHeader({ title, description, actions }: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="wb-page-header">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="wb-page-actions">{actions}</div>}
    </header>
  )
}

export function EmptyState({ title, description, action }: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="wb-empty-state">
      <div className="wb-empty-mark" aria-hidden="true">○</div>
      <h2>{title}</h2>
      {description && <p>{description}</p>}
      {action && <div className="wb-empty-action">{action}</div>}
    </div>
  )
}

const STATUS_LABELS: Record<string, string> = {
  received: '已接收',
  planning: '规划中',
  needs_clarification: '需要确认',
  awaiting_approval: '待审批',
  queued: '已排队',
  running: '执行中',
  verifying: '验证中',
  ready_for_review: '待复核',
  publishing: '发布中',
  published: '已发布',
  // 与 needs_clarification 区分：needs_human 是运行自己停下了，
  // 通常没有问题要问人（triage.questions 为空），不要都叫「需要确认」。
  needs_human: '等人介入',
  failed: '失败',
  cancelled: '已取消',
  pending: '待处理',
  completed: '已完成',
  // 任务级 blocked = 上游没通过、本任务从未开工，不是自身失败。
  blocked: '未开始',
  verified: '已验证',
}

export function statusLabel(status?: string | null): string {
  if (!status) return '未知状态'
  return STATUS_LABELS[status] ?? status
}

function statusTone(status?: string | null): string {
  if (!status) return 'neutral'
  if (['published', 'completed', 'verified'].includes(status)) return 'success'
  // blocked 不算 danger：任务被上游挡住而未开工，红色会被误读成执行失败。
  if (['failed', 'cancelled'].includes(status)) return 'danger'
  if (status === 'blocked') return 'neutral'
  if (['needs_clarification', 'awaiting_approval', 'ready_for_review', 'needs_human'].includes(status)) return 'warning'
  if (['running', 'planning', 'queued', 'verifying'].includes(status)) return 'active'
  return 'neutral'
}

export function StatusBadge({ status }: { status?: string | null }) {
  return <span className={`wb-status wb-status-${statusTone(status)}`}><i aria-hidden="true" />{statusLabel(status)}</span>
}

export function ErrorNotice({ message }: { message: string }) {
  return <div className="wb-error" role="alert"><strong>请求没有完成</strong><span>{message}</span></div>
}

export function formatDate(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false, year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function errorText(error: unknown): string {
  if (error && typeof error === 'object' && 'detail' in error && typeof error.detail === 'string') return error.detail
  return error instanceof Error ? error.message : '网络请求失败，请稍后重试。'
}

