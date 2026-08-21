import { useCallback, useEffect, useState } from 'react'
import type { Attempt } from '../types'
import { resolutionText, roleText } from '../lib/humanize'
import { explainClaim, allEnvIssues } from '../lib/explainClaim'
import {
  fmtCost,
  fmtDuration,
  fmtTime,
  resCode,
  resColor,
  summarize,
  verdictSummary,
} from '../lib/attemptFormat'
import { KeyBar, Panel } from './ConsoleChrome'
import PermissionPanel from './PermissionPanel'

/** 六列网格。表头和数据行必须共用，否则列会错位。 */
const COLS =
  'grid grid-cols-[2.5rem_4.5rem_5rem_5rem_7rem_1fr] items-baseline gap-2'

interface AttemptTimelineProps {
  attempts: Attempt[]
}

export default function AttemptTimeline({ attempts }: AttemptTimelineProps) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [failOnly, setFailOnly] = useState(false)

  const toggle = useCallback((no: number) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      next.has(no) ? next.delete(no) : next.add(no)
      return next
    })
  }, [])

  // e = 展开/折叠全部，f = 只看失败轮。绑在 window 上，
  // 但输入框聚焦时不拦（否则没法在别处打字）。
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const el = ev.target as HTMLElement | null
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return

      if (ev.key === 'e') {
        setExpanded((prev) =>
          prev.size === attempts.length
            ? new Set()
            : new Set(attempts.map((a) => a.attempt_no)),
        )
      } else if (ev.key === 'f') {
        setFailOnly((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [attempts])

  if (attempts.length === 0) {
    return (
      <Panel title="attempts (0)">
        <p className="px-3 py-2 font-mono text-xs text-slate-500">
          [ no attempts ] 这个任务还没派过工。
        </p>
      </Panel>
    )
  }

  const sorted = [...attempts].sort((a, b) => a.attempt_no - b.attempt_no)
  const stat = summarize(sorted)
  const allExpanded = expanded.size === sorted.length
  const shown = failOnly
    ? sorted.filter((a) => a.verdicts.some((v) => v.verdict === 'fail'))
    : sorted

  return (
    <Panel
      title={`attempts (${stat.total})`}
      meta={
        stat.executed === stat.total
          ? `${stat.executed} 轮全部真实执行`
          : `${stat.executed} 轮真实执行 · ${stat.total - stat.executed} 轮未派工`
      }
      footer={
        <KeyBar
          hints={[
            { key: 'e', action: allExpanded ? '折叠全部' : '展开全部' },
            {
              key: 'f',
              action: failOnly
                ? `只看失败轮 (${shown.length}/${stat.total}) · 按 f 看全部`
                : '只看失败轮',
            },
            { key: '点击行', action: '展开单轮明细' },
          ]}
        />
      }
    >
      <div className="font-mono text-xs">
        <div className={`${COLS} border-b border-slate-200 bg-slate-200/60 px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-700`}>
          <span>#</span>
          <span>model</span>
          <span className="text-right">cost</span>
          <span className="text-right">time</span>
          <span>status</span>
          <span>verdicts</span>
        </div>

        {shown.length === 0 ? (
          <p className="px-3 py-2 text-slate-500">
            [ filtered ] 没有失败轮次，{stat.total} 轮全部通过。按 f 看全部。
          </p>
        ) : (
          shown.map((attempt) => (
            <AttemptRow
              key={attempt.attempt_no}
              attempt={attempt}
              open={expanded.has(attempt.attempt_no)}
              onToggle={() => toggle(attempt.attempt_no)}
            />
          ))
        )}
      </div>
    </Panel>
  )
}

function AttemptRow({
  attempt,
  open,
  onToggle,
}: {
  attempt: Attempt
  open: boolean
  onToggle: () => void
}) {
  const res = resolutionText(attempt.resolution)
  const notDispatched = attempt.resolution === 'not_dispatched'

  const failedClaims = attempt.verdicts
    .filter((v) => v.verdict === 'fail')
    .flatMap((v) => v.claims)
  const envOnly = allEnvIssues(failedClaims)

  return (
    <div
      className={`border-b border-slate-100 last:border-b-0 ${
        notDispatched ? 'bg-slate-50/60' : 'bg-white'
      }`}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className={`${COLS} w-full px-3 py-1.5 text-left text-slate-700 hover:bg-sky-50`}
      >
        <span className="text-slate-400">
          {open ? '▾' : '▸'}
          {attempt.attempt_no}
        </span>
        <span className="font-semibold">{attempt.model}</span>
        <span className="text-right tabular-nums">{fmtCost(attempt.cost_usd)}</span>
        <span className="text-right tabular-nums text-slate-500">
          {fmtDuration(attempt.wall_clock_s)}
        </span>
        <span className={`font-semibold ${resColor(attempt.resolution)}`}>
          [{resCode(attempt.resolution)}]
        </span>
        <span className="truncate text-slate-500">{verdictSummary(attempt)}</span>
      </button>

      {open && (
        <div className="border-t border-slate-200 bg-slate-50/50 px-3 py-2">
          <div className="mb-2 space-y-0.5 text-[10px] text-slate-500">
            <div>
              {fmtTime(attempt.created_at)} · tok {attempt.tokens_in.toLocaleString()}/
              {attempt.tokens_out.toLocaleString()}
            </div>
            <div>
              {attempt.harness}@{attempt.harness_version}
              {attempt.commit && <> · {attempt.commit.slice(0, 8)}</>}
            </div>
          </div>

          <div className="space-y-1 text-[11px] leading-relaxed">
            <div className="text-slate-700">{res.what}</div>
            {envOnly && !notDispatched && (
              <div className="text-sky-700">ⓘ ENV ISSUE (not AI quality)</div>
            )}
          </div>

          <div className="mt-2 space-y-1">
            {attempt.verdicts.map((v, i) => (
              <VerdictLine key={`${v.role}-${i}`} verdict={v} />
            ))}
          </div>

          <PermissionPanel events={attempt.permission_events} />
        </div>
      )}
    </div>
  )
}

function VerdictLine({ verdict }: { verdict: { role: string; verdict: string; claims: any[] } }) {
  const [showClaims, setShowClaims] = useState(false)
  const role = roleText(verdict.role)
  const icon = verdict.verdict === 'pass' ? '✓' : '✗'
  const color = verdict.verdict === 'pass' ? 'text-emerald-600' : 'text-rose-600'

  if (verdict.verdict === 'pass') {
    return (
      <div className={`${color} text-[11px]`}>
        {icon} {role.name} — PASS
      </div>
    )
  }

  return (
    <div className="text-[11px]">
      <button
        type="button"
        onClick={() => setShowClaims((v) => !v)}
        className={`${color} hover:underline`}
      >
        {icon} {role.name} — FAIL ({verdict.claims.length})
      </button>
      {showClaims && (
        <div className="ml-3 mt-1 space-y-1.5 border-l-2 border-rose-200 pl-2 text-slate-700">
          {verdict.claims.map((c, i) => {
            const ex = explainClaim(c)
            return (
              <div key={i} className="space-y-0.5">
                <div className="font-medium text-slate-800">{ex.title}</div>
                {ex.action && <div className="text-slate-600">→ {ex.action}</div>}
                <details className="text-[10px] text-slate-500">
                  <summary className="cursor-pointer hover:text-slate-700">
                    [raw record]
                  </summary>
                  <div className="ml-2 mt-1 space-y-0.5 font-mono">
                    <div>check: {c.check}</div>
                    {c.expected && <div>expected: {c.expected}</div>}
                    {(c.got || c.actual) && (
                      <div className="break-all">got: {c.got || c.actual}</div>
                    )}
                  </div>
                </details>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
