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

  return (
    // 方角 + 单像素边 + 左侧状态色条：终端列表项，不是卡片。
    // hover 用底色变化而不是阴影浮起——阴影在密排列表里会互相干扰。
    <Link
      to={`/task/${encodeURIComponent(task.task_id)}`}
      className={
        'block border-l-2 border-y border-r border-slate-200 bg-white ' +
        'px-2 py-1.5 font-mono transition hover:bg-sky-50/60 ' +
        'focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-600 ' +
        (isRunning ? 'border-l-cyan-600' : 'border-l-slate-300')
      }
    >
      <div className="flex items-baseline gap-1.5">
        {/* 行首提示符：终端里每条记录的起始标记 */}
        <span className="shrink-0 text-slate-400" aria-hidden="true">
          {isRunning ? '>' : '$'}
        </span>
        <span className="min-w-0 flex-1 break-all text-xs font-semibold text-slate-800">
          {task.task_id}
        </span>
        {task.resolution !== undefined && (
          <ResolutionBadge resolution={task.resolution} className="shrink-0" />
        )}
      </div>

      <p className="mt-0.5 line-clamp-2 pl-3 text-xs leading-snug text-slate-500">
        {task.prompt_preview}
      </p>

      {/* 数据行：字段名淡、值重、数字等宽对齐，一行扫完 */}
      <div className="mt-1 flex flex-wrap items-baseline gap-x-3 pl-3 text-xs tabular-nums">
        <span className="text-slate-400">
          chk <span className="font-medium text-slate-700">{task.checks_count}</span>
        </span>
        {task.attempts_count !== undefined && (
          <span className="text-slate-400">
            rnd{' '}
            <span className="font-medium text-slate-700">{task.attempts_count}</span>
          </span>
        )}
        {task.total_cost_usd !== undefined && (
          <span className="text-slate-400">
            cost{' '}
            <span className="font-medium text-amber-700">
              {formatCost(task.total_cost_usd)}
            </span>
          </span>
        )}
        <time
          dateTime={task.mtime}
          title={absoluteTime}
          className="ml-auto text-slate-400"
        >
          {relativeTime(task.mtime)}
        </time>
      </div>
    </Link>
  )
}

export default TaskCard
