import { useState } from 'react'
import type { Claim, Verdict } from '../types'
import { roleText } from '../lib/humanize'
import { explainClaim, summarizeClaims } from '../lib/explainClaim'

interface VerdictPanelProps {
  verdicts: Verdict[]
}

/**
 * 监工判词。默认就说人话：谁不满意 + 一句话结论。
 * 「原始记录」折叠在里面，给维护者排查用，操作者不用点开也能看懂。
 */
export default function VerdictPanel({ verdicts }: VerdictPanelProps) {
  if (verdicts.length === 0) {
    return <div className="text-xs italic text-slate-400">本轮没有监工记录</div>
  }

  const sorted = [...verdicts].sort((a, b) => {
    const rank = (v: Verdict) => (v.verdict === 'fail' ? 0 : 1)
    return rank(a) - rank(b)
  })

  return (
    <ul className="space-y-1.5">
      {sorted.map((verdict, idx) => (
        <VerdictRow key={`${verdict.role}-${idx}`} verdict={verdict} />
      ))}
    </ul>
  )
}

function VerdictRow({ verdict }: { verdict: Verdict }) {
  const pass = verdict.verdict === 'pass'
  const role = roleText(verdict.role)

  if (pass) {
    return (
      <li className="flex items-center gap-2 px-1 text-xs">
        <span className="text-emerald-600">✓</span>
        <span className="text-slate-600">{role.name}</span>
        <span className="text-slate-400">通过</span>
        <span className="hidden text-slate-300 sm:inline">· {role.duty}</span>
      </li>
    )
  }

  return (
    <li className="rounded-md border border-rose-200 bg-rose-50/60 p-2.5">
      <div className="flex items-start gap-2">
        <span className="mt-px text-rose-600">✗</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-xs font-medium text-rose-800">{role.name}</span>
            <span className="text-[11px] text-rose-400">否决 · {role.duty}</span>
          </div>
          <div className="mt-1 text-[13px] font-medium leading-snug text-slate-800">
            {summarizeClaims(verdict.claims) || '监工否决了，但没有留下具体判词'}
          </div>
          {verdict.claims.map((claim, i) => (
            <ClaimExplained key={`${claim.check}-${i}`} claim={claim} />
          ))}
        </div>
      </div>
    </li>
  )
}

const KIND_TAG: Record<string, { text: string; cls: string }> = {
  env: {
    text: '环境问题，不是 AI 干得不好',
    cls: 'bg-sky-100 text-sky-800',
  },
  policy: {
    text: '规则拦下的',
    cls: 'bg-violet-100 text-violet-800',
  },
  quality: {
    text: '产出质量不达标',
    cls: 'bg-amber-100 text-amber-800',
  },
  unknown: {
    text: '未登记的检查',
    cls: 'bg-slate-200 text-slate-700',
  },
}

function ClaimExplained({ claim }: { claim: Claim }) {
  const [raw, setRaw] = useState(false)
  const ex = explainClaim(claim)
  const tag = KIND_TAG[ex.kind]
  const got = claim.actual ?? claim.got

  return (
    <div className="mt-2 border-l-2 border-rose-200 pl-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className={'rounded px-1.5 py-0.5 text-[11px] font-medium ' + tag.cls}>
          {tag.text}
        </span>
      </div>

      {ex.why && (
        <p className="mt-1 text-xs leading-relaxed text-slate-600">{ex.why}</p>
      )}

      {ex.action && (
        <p className="mt-1.5 text-xs leading-relaxed text-slate-800">
          <span className="font-medium text-slate-500">你可以：</span> {ex.action}
        </p>
      )}

      <button
        type="button"
        onClick={() => setRaw((v) => !v)}
        aria-expanded={raw}
        className="mt-1.5 rounded text-[11px] text-slate-400 underline decoration-dotted hover:text-slate-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
      >
        {raw ? '收起原始记录' : '原始记录（给维护者）'}
      </button>

      {raw && (
        <dl className="mt-1.5 space-y-1 rounded border border-slate-200 bg-white p-2">
          <RawField label="检查名" value={claim.check} mono />
          {claim.command ? (
            <RawField label="命令" value={claim.command} mono />
          ) : null}
          {claim.expected ? (
            <RawField label="要求" value={claim.expected} />
          ) : null}
          {got ? <RawField label="实际" value={got} mono /> : null}
        </dl>
      )}
    </div>
  )
}

function RawField({
  label,
  value,
  mono = false,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex gap-2">
      <dt className="w-12 shrink-0 text-[11px] text-slate-400">{label}</dt>
      <dd
        className={
          'min-w-0 flex-1 whitespace-pre-wrap break-all text-slate-700' +
          (mono ? ' font-mono text-[11px]' : ' text-[11px]')
        }
      >
        {value}
      </dd>
    </div>
  )
}
