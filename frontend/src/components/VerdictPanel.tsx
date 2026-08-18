import { useState } from 'react'
import type { Claim, Verdict, VerdictRole } from '../types'

const ROLE_LABEL: Record<VerdictRole, string> = {
  scope: '范围监工',
  regression: '回归监工',
  risk: '风险监工',
  beacon: '灯塔监工',
}

function roleLabel(role: VerdictRole | string): string {
  return ROLE_LABEL[role as VerdictRole] ?? role
}

/** claims 里 actual / got 两种字段名都可能出现（契约文档 vs 真实后端）。 */
function claimActual(claim: Claim): string | undefined {
  return claim.actual ?? claim.got
}

interface VerdictPanelProps {
  verdicts: Verdict[]
}

/**
 * 监工判词。每条判词一行，fail 且带 claims 时可展开，
 * 展开后按 check / command / expected / actual 分行显示，不是 raw JSON。
 */
export default function VerdictPanel({ verdicts }: VerdictPanelProps) {
  if (verdicts.length === 0) {
    return (
      <div className="text-xs text-slate-400 italic">本轮没有监工判词记录</div>
    )
  }

  return (
    <ul className="space-y-1">
      {verdicts.map((verdict, idx) => (
        <VerdictRow
          key={`${verdict.role}-${idx}`}
          verdict={verdict}
          isLast={idx === verdicts.length - 1}
        />
      ))}
    </ul>
  )
}

function VerdictRow({ verdict, isLast }: { verdict: Verdict; isLast: boolean }) {
  const [open, setOpen] = useState(false)
  const pass = verdict.verdict === 'pass'
  const expandable = verdict.claims.length > 0

  const head = (
    <>
      <span className="select-none font-mono text-slate-300">
        {isLast ? '└' : '├'}
      </span>
      <span className="w-28 shrink-0 text-slate-600">
        {roleLabel(verdict.role)}
      </span>
      <span
        className={
          'inline-flex items-center gap-1 font-medium ' +
          (pass ? 'text-emerald-600' : 'text-rose-600')
        }
      >
        {pass ? '✓ pass' : '✗ fail'}
      </span>
      {expandable && (
        <span className="text-slate-400">
          <span className="inline-block w-3 select-none">
            {open ? '▾' : '▸'}
          </span>
          {open ? '收起判词' : `展开看判词（${verdict.claims.length} 条）`}
        </span>
      )}
    </>
  )

  return (
    <li className="text-xs">
      {expandable ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
        >
          {head}
        </button>
      ) : (
        <div className="flex items-center gap-2 px-1 py-0.5">{head}</div>
      )}

      {open && (
        <div className="ml-6 mt-1 space-y-2 border-l-2 border-rose-200 pl-3">
          {verdict.claims.map((claim, i) => (
            <ClaimCard key={`${claim.check}-${i}`} claim={claim} />
          ))}
        </div>
      )}
    </li>
  )
}

function ClaimCard({ claim }: { claim: Claim }) {
  const actual = claimActual(claim)
  return (
    <div className="rounded border border-slate-200 bg-slate-50 p-2">
      <div className="mb-1 font-semibold text-slate-700">{claim.check}</div>
      <dl className="space-y-1">
        {claim.command ? (
          <ClaimField label="command" value={claim.command} mono />
        ) : null}
        {claim.expected ? (
          <ClaimField label="expected" value={claim.expected} tone="expected" />
        ) : null}
        {actual ? (
          <ClaimField label="actual" value={actual} tone="actual" mono />
        ) : null}
      </dl>
    </div>
  )
}

function ClaimField({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string
  value: string
  mono?: boolean
  tone?: 'expected' | 'actual'
}) {
  const toneClass =
    tone === 'expected'
      ? 'text-emerald-700'
      : tone === 'actual'
        ? 'text-rose-700'
        : 'text-slate-700'
  return (
    <div className="flex gap-2">
      <dt className="w-16 shrink-0 text-[11px] uppercase tracking-wide text-slate-400">
        {label}
      </dt>
      <dd
        className={
          'min-w-0 flex-1 whitespace-pre-wrap break-all ' +
          toneClass +
          (mono ? ' font-mono text-[11px]' : '')
        }
      >
        {value}
      </dd>
    </div>
  )
}
