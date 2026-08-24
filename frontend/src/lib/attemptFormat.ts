// 轮次表格的展示层格式化。抄 k9s 的路子：状态是短代码 + 颜色，
// 数字右对齐等宽，缺值一律 —（不是 0，0 会被读成「跑了但免费」）。

import type { Attempt } from '../types'
import { roleText } from './humanize'

/** resolution → 短代码。表格列宽有限，用大写代码而不是句子。 */
export const RES_CODE: Record<string, string> = {
  merged: 'OK',
  reworked: 'REWORK',
  escalated: 'ESCALATE',
  blocked: 'BLOCKED',
  not_dispatched: 'SKIP',
  pending: 'RUNNING',
}

/** 状态代码的配色。抄 k9s：绿=成、红=要人管、黄=系统还在自己转、灰=没跑。 */
export const RES_COLOR: Record<string, string> = {
  merged: 'text-emerald-700',
  reworked: 'text-amber-700',
  escalated: 'text-rose-700',
  blocked: 'text-rose-700',
  not_dispatched: 'text-slate-400',
  pending: 'text-sky-700',
}

export function resCode(resolution: string): string {
  return RES_CODE[resolution] ?? resolution.toUpperCase()
}

export function resColor(resolution: string): string {
  return RES_COLOR[resolution] ?? 'text-slate-600'
}

const TIME_FMT = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function fmtTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : TIME_FMT.format(d)
}

/** 0 或缺值 → —。不显示 0s，那会谎报「秒开」。 */
export function fmtDuration(s: number): string {
  if (!s || s <= 0) return '—'
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rest = Math.round(s % 60)
  return rest ? `${m}m${rest}s` : `${m}m`
}

/** 0 或缺值 → —。空跑轮次显示 $0.00 会被误读成「跑了而且免费」。 */
export function fmtCost(usd: number): string {
  return usd > 0 ? `$${usd.toFixed(2)}` : '—'
}

/** 判据摘要：✓风险 ✗回归 ✓范围。列宽有限，去掉「监工」二字。 */
export function verdictSummary(attempt: Attempt): string {
  return attempt.verdicts
    .map((v) => `${v.verdict === 'pass' ? '✓' : '✗'}${shortRole(v.role)}`)
    .join(' ')
}

/** 复用 humanize 的监工查表，去掉「监工」二字塞进窄列。 */
export function shortRole(role: string): string {
  return roleText(role).name.replace('监工', '')
}

/** 任务级汇总，喂给顶部状态栏。 */
export function summarize(attempts: Attempt[]) {
  const real = attempts.filter((a) => (a.cost_usd ?? 0) > 0)
  return {
    total: attempts.length,
    executed: real.length,
    cost: attempts.reduce((s, a) => s + (a.cost_usd ?? 0), 0),
    wall: attempts.reduce((s, a) => s + (a.wall_clock_s ?? 0), 0),
    failedRounds: attempts.filter((a) =>
      a.verdicts.some((v) => v.verdict === 'fail'),
    ).length,
  }
}
