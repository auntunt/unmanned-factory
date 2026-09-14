import { useState, type KeyboardEvent } from 'react'
import type { Run } from '../workspace/types'
import { formatDate, statusLabel } from './ui'
import './presentation.css'

// 一个视口内不重复整句指引；边界说明后置。内部术语必须附白话解释。
export function runTitle(run: Pick<Run, 'id' | 'request' | 'plan'>): string {
  const first = run.request?.trim().split(/\r?\n/)[0]?.trim()
  const title = [first, run.plan?.title?.trim()].find(value => value && !['持续编码', 'continuous', '自动编码'].includes(value))
  return title ? Array.from(title).slice(0, 40).join('') : `运行 ${String(run.id).slice(0, 8)}`
}
export function RunMode({ run }: { run: Run }) {
  return run.execution_mode === 'continuous' ? <span className="wb-mode-badge">持续编码</span> : null
}
export function CopyValue({ value, label = '编号', length = 8, display }: { value: string | number; label?: string; length?: number; display?: string }) {
  const [message, setMessage] = useState('')
  return <span className="wb-copy-value"><button type="button" title={`复制完整${label}`} aria-label={`复制完整${label}`} onClick={() => { void (async () => { try { await navigator.clipboard.writeText(String(value)); setMessage('已复制') } catch { setMessage('复制失败，请重试') } })() }}>{display || String(value).slice(0, length)} <span aria-hidden="true">⧉</span></button><span role="status" className="wb-copy-feedback">{message}</span></span>
}
export function LoadingCard({ label = '正在读取…' }: { label?: string }) {
  return <div className="wb-card wb-skeleton" role="status" aria-label={label}><span /><span /><span /></div>
}
export function relativeTime(value: string) {
  const at = Date.parse(value)
  if (!Number.isFinite(at)) return '时间未记录'
  const hours = Math.floor(Math.max(0, Date.now() - at) / 3600000)
  return hours < 1 ? '不足 1 小时前' : hours < 24 ? `${hours} 小时前` : new Date(at).toLocaleDateString('zh-CN')
}
export function ListTime({ value }: { value: string }) {
  return <time dateTime={Number.isFinite(Date.parse(value)) ? value : undefined} title={formatDate(value)}>{relativeTime(value)}</time>
}
export function StatusDot({ status }: { status?: string }) {
  const tone = ['pass', 'published', 'ready_for_review', 'verified', 'completed', 'inspection_completed', 'ready'].includes(status || '') ? 'success' : ['fail', 'failed', 'inspection_failed'].includes(status || '') ? 'danger' : ['running', 'planning', 'queued', 'verifying', 'publishing'].includes(status || '') ? 'active' : ['unverified', 'needs_human', 'needs_clarification', 'awaiting_approval'].includes(status || '') ? 'warning' : 'neutral'
  return <span className={`wb-state-dot is-${tone}`}><i aria-hidden="true" />{status ? statusLabel(status) : '未验证'}</span>
}
export function tabKeys(event: KeyboardEvent<HTMLElement>) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]:not(:disabled)'))
  const index = tabs.indexOf(document.activeElement as HTMLElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  event.preventDefault(); tabs[next]?.focus(); tabs[next]?.click()
}
