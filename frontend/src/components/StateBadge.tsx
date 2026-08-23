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

// TUI 配色（k9s/lazydocker）：底色不填，靠文字色区分，深色高对比。
// 浅底上填色块会把信息密度拉低——一屏五桶时那些色块比数据更抢眼。
export const STATE_BADGE_CLASSES: Record<QueueState, string> = {
  inbox: 'text-slate-500',
  running: 'text-cyan-700',
  done: 'text-emerald-700',
  needs_human: 'text-amber-700',
  blocked: 'text-red-700',
}

// 桶标题的状态色（原来是圆点，现在给方括号状态码上色）
export const STATE_DOT_CLASSES: Record<QueueState, string> = {
  inbox: 'text-slate-500',
  running: 'text-cyan-700',
  done: 'text-emerald-700',
  needs_human: 'text-amber-700',
  blocked: 'text-red-700',
}

// 终端里的状态记号。ASCII 优先——等宽字体下宽度稳定，不会把表格挤歪。
export const STATE_ICONS: Record<QueueState, string> = {
  inbox: '·',
  running: '>',
  done: 'OK',
  needs_human: '!',
  blocked: 'X',
}

export const RESOLUTION_LABELS: Record<Resolution, string> = {
  merged: '已合并',
  reworked: '已返工',
  escalated: '已升级',
  blocked: '已阻塞',
  not_dispatched: '未派发',
}

const RESOLUTION_BADGE_CLASSES: Record<Resolution, string> = {
  merged: 'text-emerald-700',
  reworked: 'text-slate-500',
  escalated: 'text-amber-700',
  blocked: 'text-red-700',
  not_dispatched: 'text-slate-400',
}

// 无圆角、无填充、等宽——徽章在终端里就是一段带色的文本。
const BASE = 'inline-flex items-center gap-1 font-mono text-xs whitespace-nowrap'

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
      [{showIcon && <span aria-hidden="true">{STATE_ICONS[state]}</span>}
      {STATE_LABELS[state]}]
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
      [{RESOLUTION_LABELS[resolution]}]
    </span>
  )
}

export default StateBadge
