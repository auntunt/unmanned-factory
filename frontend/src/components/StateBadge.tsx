import type { QueueState, Resolution } from '../types'

// 状态配色/文案的唯一来源。T04 详情页直接 import 这里的常量，
// 避免两个页面各写一套 class 名导致颜色漂移。
export const STATE_LABELS: Record<QueueState, string> = {
  inbox: '待处理',
  running: '执行中',
  done: '已完成',
  needs_human: '待人工',
  blocked: '已阻塞',
}

export const STATE_BADGE_CLASSES: Record<QueueState, string> = {
  inbox: 'bg-slate-100 text-slate-600',
  running: 'bg-blue-50 text-blue-700 animate-pulse',
  done: 'bg-emerald-50 text-emerald-700',
  needs_human: 'bg-amber-50 text-amber-700',
  blocked: 'bg-red-50 text-red-700',
}

// 桶标题上的圆点，用实色以便在浅色徽章旁仍能分辨
export const STATE_DOT_CLASSES: Record<QueueState, string> = {
  inbox: 'bg-slate-400',
  running: 'bg-blue-500 animate-pulse',
  done: 'bg-emerald-500',
  needs_human: 'bg-amber-500',
  blocked: 'bg-red-500',
}

export const STATE_ICONS: Record<QueueState, string> = {
  inbox: '·',
  running: '⟳',
  done: '✓',
  needs_human: '⚠',
  blocked: '✕',
}

export const RESOLUTION_LABELS: Record<Resolution, string> = {
  merged: '已合并',
  reworked: '已返工',
  escalated: '已升级',
  blocked: '已阻塞',
  not_dispatched: '未派发',
}

const RESOLUTION_BADGE_CLASSES: Record<Resolution, string> = {
  merged: 'bg-emerald-50 text-emerald-700',
  reworked: 'bg-slate-100 text-slate-600',
  escalated: 'bg-amber-50 text-amber-700',
  blocked: 'bg-red-50 text-red-700',
  not_dispatched: 'bg-slate-100 text-slate-500',
}

const BASE = 'inline-flex items-center gap-1 rounded-full px-2 py-0.5 ' +
  'text-xs font-medium whitespace-nowrap'

export interface StateBadgeProps {
  state: QueueState
  /** 是否显示状态图标，默认显示 */
  showIcon?: boolean
  className?: string
}

/** 队列状态徽章：inbox 灰 / running 蓝+脉冲 / done 绿 / needs_human 橙 / blocked 红 */
export function StateBadge({
  state,
  showIcon = true,
  className = '',
}: StateBadgeProps) {
  return (
    <span
      className={`${BASE} ${STATE_BADGE_CLASSES[state]} ${className}`}
      title={`状态：${STATE_LABELS[state]}`}
    >
      {showIcon && <span aria-hidden="true">{STATE_ICONS[state]}</span>}
      {STATE_LABELS[state]}
    </span>
  )
}

export interface ResolutionBadgeProps {
  resolution: Resolution
  className?: string
}

/** 最终处置结果徽章（done / needs_human 的任务才有） */
export function ResolutionBadge({
  resolution,
  className = '',
}: ResolutionBadgeProps) {
  return (
    <span
      className={`${BASE} ${RESOLUTION_BADGE_CLASSES[resolution]} ${className}`}
      title={`处置：${RESOLUTION_LABELS[resolution]}`}
    >
      {RESOLUTION_LABELS[resolution]}
    </span>
  )
}

export default StateBadge
