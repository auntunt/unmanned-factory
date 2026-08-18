import type { Attempt, Resolution } from '../types'
import VerdictPanel from './VerdictPanel'

const RESOLUTION_STYLE: Record<Resolution, string> = {
  merged: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  reworked: 'bg-amber-100 text-amber-800 border-amber-300',
  escalated: 'bg-orange-100 text-orange-800 border-orange-300',
  blocked: 'bg-rose-100 text-rose-800 border-rose-300',
  not_dispatched: 'bg-slate-100 text-slate-500 border-slate-300 border-dashed',
}

const RESOLUTION_DOT: Record<Resolution, string> = {
  merged: 'bg-emerald-500',
  reworked: 'bg-amber-500',
  escalated: 'bg-orange-500',
  blocked: 'bg-rose-500',
  not_dispatched: 'bg-slate-300',
}

const MODEL_STYLE: Record<string, string> = {
  haiku: 'bg-sky-100 text-sky-800',
  sonnet: 'bg-violet-100 text-violet-800',
  opus: 'bg-fuchsia-100 text-fuchsia-800',
}

const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩']

function circled(n: number): string {
  return CIRCLED[n - 1] ?? String(n)
}

function modelStyle(model: string): string {
  return MODEL_STYLE[model] ?? 'bg-slate-100 text-slate-700'
}

const TIME_FMT = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

function formatCreatedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return TIME_FMT.format(d)
}

function formatSeconds(s: number): string {
  if (s <= 0) return '—'
  if (s < 60) return `${s.toFixed(0)}s`
  const m = Math.floor(s / 60)
  const rest = Math.round(s % 60)
  return `${m}m${String(rest).padStart(2, '0')}s`
}

function formatCost(usd: number): string {
  return usd > 0 ? `$${usd.toFixed(2)}` : '$0.00'
}

interface AttemptTimelineProps {
  attempts: Attempt[]
}

/** 轮次级垂直时间线。粒度到 attempt，不再往下拆。 */
export default function AttemptTimeline({ attempts }: AttemptTimelineProps) {
  if (attempts.length === 0) {
    return (
      <section className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-6 text-center text-sm text-slate-500">
        这个任务还没有任何执行轮次。
      </section>
    )
  }

  const sorted = [...attempts].sort((a, b) => a.attempt_no - b.attempt_no)

  return (
    <section aria-label="轮次时间线" className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-700">
          轮次时间线（{sorted.length} 轮）
        </h2>
        <span className="text-xs text-slate-400">点击 fail 判词可展开详情</span>
      </div>

      <ol className="relative space-y-3 before:absolute before:bottom-4 before:left-[0.6875rem] before:top-4 before:w-px before:bg-slate-200">
        {sorted.map((attempt) => (
          <AttemptCard key={attempt.attempt_no} attempt={attempt} />
        ))}
      </ol>
    </section>
  )
}

function AttemptCard({ attempt }: { attempt: Attempt }) {
  const notDispatched = attempt.resolution === 'not_dispatched'
  return (
    <li className="relative pl-8">
      <span
        aria-hidden="true"
        className={
          'absolute left-1.5 top-3 h-2.5 w-2.5 rounded-full ring-2 ring-white ' +
          RESOLUTION_DOT[attempt.resolution]
        }
      />
      <div
        className={
          'rounded-md border p-3 ' +
          (notDispatched
            ? 'border-dashed border-slate-300 bg-slate-50'
            : 'border-slate-200 bg-white')
        }
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="font-mono text-sm text-slate-400">
            {circled(attempt.attempt_no)}
          </span>
          <span className="text-sm font-semibold text-slate-800">
            attempt {attempt.attempt_no}
          </span>
          <span
            className={
              'rounded px-1.5 py-0.5 text-[11px] font-medium ' +
              modelStyle(attempt.model)
            }
          >
            {attempt.model}
          </span>
          <span
            className={
              'rounded border px-1.5 py-0.5 text-[11px] font-medium ' +
              RESOLUTION_STYLE[attempt.resolution]
            }
          >
            {attempt.resolution}
          </span>
          {attempt.commit && (
            <span
              className="rounded bg-emerald-50 px-1.5 py-0.5 font-mono text-[11px] text-emerald-700"
              title={attempt.commit}
            >
              {attempt.commit.slice(0, 8)}
            </span>
          )}

          <span className="ml-auto flex items-center gap-3 font-mono text-xs tabular-nums text-slate-600">
            <span className="w-16 text-right">{formatCost(attempt.cost_usd)}</span>
            <span className="w-16 text-right">
              {formatSeconds(attempt.wall_clock_s)}
            </span>
          </span>
        </div>

        <div className="mt-1 flex flex-wrap gap-x-4 text-[11px] text-slate-400">
          <span>{formatCreatedAt(attempt.created_at)}</span>
          <span className="font-mono">
            tok {attempt.tokens_in.toLocaleString()} in /{' '}
            {attempt.tokens_out.toLocaleString()} out
          </span>
          {notDispatched && (
            <span className="text-slate-500">未派工（派工前被风险监工拦下）</span>
          )}
        </div>

        <div className="mt-2">
          <VerdictPanel verdicts={attempt.verdicts} />
        </div>
      </div>
    </li>
  )
}
