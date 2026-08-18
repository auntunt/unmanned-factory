import { Link } from 'react-router-dom'
import type { QueueState, TaskSummary } from '../types'
import { ResolutionBadge } from './StateBadge'

// 后端 mtime 形如 "2026-08-18T05:26:49"（ISO 但不带时区），
// 按服务器本地时间解读即可 —— 直接交给 Date 构造函数处理。
function parseMtime(iso: string): Date | null {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

const rtf = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' })

const UNITS: { unit: Intl.RelativeTimeFormatUnit; seconds: number }[] = [
  { unit: 'year', seconds: 31536000 },
  { unit: 'month', seconds: 2592000 },
  { unit: 'day', seconds: 86400 },
  { unit: 'hour', seconds: 3600 },
  { unit: 'minute', seconds: 60 },
]

/** 相对时间，如「3 小时前」。用 Intl.RelativeTimeFormat，不引 dayjs。 */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = parseMtime(iso)
  if (!then) return iso
  const diffSec = Math.round((then.getTime() - now.getTime()) / 1000)
  const abs = Math.abs(diffSec)
  if (abs < 60) return '刚刚'
  for (const { unit, seconds } of UNITS) {
    if (abs >= seconds) {
      return rtf.format(Math.round(diffSec / seconds), unit)
    }
  }
  return '刚刚'
}

function formatCost(usd: number): string {
  return `$${usd.toFixed(2)}`
}

export interface TaskCardProps {
  task: TaskSummary
  state: QueueState
}

/** 看板上的一张任务卡片。整卡可点，跳 /task/:task_id */
export function TaskCard({ task, state }: TaskCardProps) {
  const isRunning = state === 'running'
  const absoluteTime = parseMtime(task.mtime)?.toLocaleString('zh-CN') ?? task.mtime
  // done / needs_human 才带成本与轮次
  const hasRunInfo =
    task.total_cost_usd !== undefined || task.attempts_count !== undefined

  return (
    <Link
      to={`/task/${encodeURIComponent(task.task_id)}`}
      className={
        'block rounded-lg border bg-white p-3 shadow-sm transition ' +
        'hover:border-slate-300 hover:shadow focus:outline-none ' +
        'focus-visible:ring-2 focus-visible:ring-blue-500 ' +
        (isRunning ? 'border-blue-200' : 'border-slate-200')
      }
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-mono text-xs font-semibold break-all text-slate-800">
          {task.task_id}
        </span>
        {isRunning && (
          <span
            className="shrink-0 text-blue-600 animate-pulse"
            aria-label="执行中"
            title="执行中"
          >
            ⟳
          </span>
        )}
      </div>

      <p className="mt-1.5 line-clamp-2 text-sm text-slate-600">
        {task.prompt_preview}
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
        <span>{task.checks_count} 条判据</span>
        <span aria-hidden="true">·</span>
        <time dateTime={task.mtime} title={absoluteTime}>
          {relativeTime(task.mtime)}
        </time>
      </div>

      {hasRunInfo && (
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-slate-100 pt-2 text-xs text-slate-500">
          {task.total_cost_usd !== undefined && (
            <span className="font-medium text-slate-700">
              {formatCost(task.total_cost_usd)}
            </span>
          )}
          {task.total_cost_usd !== undefined &&
            task.attempts_count !== undefined && <span aria-hidden="true">·</span>}
          {task.attempts_count !== undefined && (
            <span>{task.attempts_count} 轮</span>
          )}
          {task.resolution !== undefined && (
            <ResolutionBadge resolution={task.resolution} />
          )}
        </div>
      )}
    </Link>
  )
}

export default TaskCard
