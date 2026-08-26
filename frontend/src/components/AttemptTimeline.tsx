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
import ArtifactViewer from './ArtifactViewer'
import InterveneBar from './InterveneBar'

/** 六列网格。表头和数据行必须共用，否则列会错位。 */
const COLS =
  'grid grid-cols-[2.5rem_4.5rem_5rem_5rem_7rem_1fr] items-baseline gap-2'

interface AttemptTimelineProps {
  attempts: Attempt[]
  /** 取现场正文和提交人工介入都要它。 */
  taskId: string
  /** 介入成功后让详情页重新拉数据。 */
  onIntervened: () => void
}

export default function AttemptTimeline({
  attempts,
  taskId,
  onIntervened,
}: AttemptTimelineProps) {
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
              taskId={taskId}
              onIntervened={onIntervened}
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
  taskId,
  onIntervened,
  open,
  onToggle,
}: {
  attempt: Attempt
  taskId: string
  onIntervened: () => void
  open: boolean
  onToggle: () => void
}) {
  // 现场正文按需加载：选了哪个标签才发请求。默认 null = 都不展开，
  // 因为大多数时候人只想看判词，150KB 的 transcript 不该无条件拉下来。
  const [tab, setTab] = useState<'transcript' | 'diff' | null>(null)

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

          <HumanRecord attempt={attempt} />

          <ArtifactTabs
            attempt={attempt}
            taskId={taskId}
            tab={tab}
            onPick={setTab}
          />
        </div>
      )}

      {open && !notDispatched && (
        <InterveneBar
          taskId={taskId}
          attemptNo={attempt.attempt_no}
          onDone={onIntervened}
        />
      )}
    </div>
  )
}

/** 定案理由 + 人工验收结果。都没有时整块不渲染。 */
function HumanRecord({ attempt }: { attempt: Attempt }) {
  const hv = attempt.human_verdict ?? ''
  const note = attempt.resolution_note ?? ''
  if (!hv && !note) return null

  return (
    <div className="mt-2 space-y-1 rounded border border-slate-200 bg-white px-2 py-1.5 text-[11px]">
      {note && (
        <div>
          <span className="text-slate-400">定案理由 </span>
          <span className="text-slate-700">{note}</span>
        </div>
      )}
      {hv && (
        <div>
          <span className="text-slate-400">人工验收 </span>
          <span
            className={
              hv === 'pass'
                ? 'font-semibold text-emerald-700'
                : 'font-semibold text-rose-700'
            }
          >
            {hv === 'pass' ? '✓ 通过' : '✗ 不通过'}
          </span>
          {attempt.human_at && (
            <span className="ml-1 text-slate-400">{fmtTime(attempt.human_at)}</span>
          )}
          {attempt.human_note && (
            <div className="mt-0.5 text-slate-700">{attempt.human_note}</div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * 执行日志 / 代码改动的标签切换。
 *
 * `has_transcript` 是 undefined 时（老后端不返回这字段）按「可能有」处理并
 * 让人点 —— 端点会告诉他到底有没有。按 false 处理会把按钮藏掉，那样人
 * 永远不知道这里能看正文。
 */
function ArtifactTabs({
  attempt,
  taskId,
  tab,
  onPick,
}: {
  attempt: Attempt
  taskId: string
  tab: 'transcript' | 'diff' | null
  onPick: (t: 'transcript' | 'diff' | null) => void
}) {
  const maybe = (v: boolean | undefined) => v !== false

  return (
    <div className="mt-2 overflow-hidden rounded border border-slate-200">
      <div className="flex items-center gap-1 bg-slate-100 px-1.5 py-1">
        <span className="mr-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          执行现场
        </span>
        {(['transcript', 'diff'] as const).map((k) => {
          const has = maybe(k === 'transcript' ? attempt.has_transcript : attempt.has_diff)
          const active = tab === k
          return (
            <button
              key={k}
              type="button"
              disabled={!has}
              onClick={() => onPick(active ? null : k)}
              aria-pressed={active}
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
                active
                  ? 'border-sky-400 bg-white text-sky-800'
                  : 'border-slate-300 bg-white text-slate-600 hover:bg-sky-50'
              } disabled:opacity-40`}
              title={has ? undefined : '这一轮没有归档这项'}
            >
              {k === 'transcript' ? '执行日志' : '代码改动'}
              {!has && ' (无)'}
            </button>
          )
        })}
      </div>
      {tab && (
        <ArtifactViewer taskId={taskId} attemptNo={attempt.attempt_no} kind={tab} />
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
