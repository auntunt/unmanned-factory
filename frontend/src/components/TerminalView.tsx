import type { CSSProperties } from 'react'
import { TERM, MONO } from '../theme/tokens'
import { NODES } from '../theme/nodes'
import type { Buckets } from '../lib/bucket'
import type { TaskRow } from './FactoryTaskCard'
import { fmtUsd, fmtRate } from '../theme/geometry'

/**
 * 终端视图 —— 闭环链路的第二档，给运维自己排查用（k9s/lazydocker 风）。
 *
 * 和环形图是同一份数据（bucket + analytics），两个受众两种呈现：
 *   环形图给老板看「链路长这样、闭环率多少」，一眼是否 nice；
 *   终端表给自己看「哪个阶段堆了几个、哪条判据白占坑、钱烧在谁身上」，一屏可操作。
 *
 * 硬约束（对齐用户反复强调的 geek 浅色风）：
 *   等宽字体全程 · 青色列头 · 琥珀数值 · 状态严格三色（绿=OK 红=失败 橙=警告）·
 *   ASCII 边框 · 信息密度拉满、一屏看完 · 不用一张卡片。
 */

interface Analytics {
  core?: any
  cost?: any
  gates?: any
  stuck?: any[]
  window_days?: number
}

interface Props {
  buckets: Buckets
  counts: Record<string, number>
  analytics: Analytics | null
  onTaskClick?: (t: TaskRow) => void
  onNodeClick?: (key: string) => void
}

// 状态语义 → 三色。派生态压灰，和环形图口径一致（0 不代表没跑）。
function stageColor(key: string, n: number): string {
  if (key === 'blocked') return n > 0 ? TERM.fail : TERM.muted
  if (key === 'needs_human') return n > 0 ? TERM.warn : TERM.muted
  if (key === 'merged') return TERM.ok
  const node = NODES.find((x) => x.key === key)
  if (node?.source === 'derived') return TERM.muted
  return n > 0 ? TERM.data : TERM.muted
}

const th: CSSProperties = {
  textAlign: 'left',
  padding: '3px 10px',
  color: TERM.header,
  fontWeight: 600,
  borderBottom: `1px solid ${TERM.border}`,
  whiteSpace: 'nowrap',
}
const td: CSSProperties = {
  padding: '3px 10px',
  color: TERM.data,
  borderBottom: `1px solid ${TERM.border}`,
  whiteSpace: 'nowrap',
}

/** ASCII 分节标题：╾─ 标题 ─────╼ ，等宽下能对齐成一条线。 */
function SectionBar({ label, right }: { label: string; right?: string }) {
  return (
    <div
      style={{
        fontFamily: MONO,
        fontSize: 12,
        color: TERM.header,
        margin: '14px 0 4px',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
      }}
    >
      <span style={{ fontWeight: 700 }}>{`▌${label}`}</span>
      <span style={{ flex: 1, borderTop: `1px dashed ${TERM.border}`, opacity: 0.8 }} />
      {right && <span style={{ color: TERM.muted, fontWeight: 400 }}>{right}</span>}
    </div>
  )
}

export default function TerminalView({ buckets, counts, analytics, onTaskClick, onNodeClick }: Props) {
  const stages = ['inbox', 'running', 'judging', 'reworking', 'done', 'needs_human', 'merged']
  const total = stages.reduce((s, k) => s + (counts[k] ?? 0), 0)
  const merged = counts.merged ?? 0
  const human = counts.needs_human ?? 0
  const blocked = counts.blocked ?? 0
  const stuck = analytics?.stuck?.length ?? 0
  const costTotal = analytics?.cost?.total_usd ?? 0
  const waste = analytics?.cost?.wasted_usd ?? null
  const auto = analytics?.core?.auto_resolved_rate?.value ?? null

  return (
    <div style={{ fontFamily: MONO, background: TERM.bg, color: TERM.data }}>
      <div
        style={{
          display: 'flex',
          gap: 8,
          flexWrap: 'wrap',
          alignItems: 'center',
          padding: '8px 10px',
          border: `1px solid ${TERM.border}`,
          borderRadius: 8,
          background: '#fff',
        }}
      >
        <span style={{ color: TERM.header, fontWeight: 700 }}>PIPELINE</span>
        <span style={{ color: TERM.muted }}>window={analytics?.window_days ?? '—'}d</span>
        <span>total <b style={{ color: TERM.accent }}>{total}</b></span>
        <span>merged <b style={{ color: TERM.ok }}>{merged}</b></span>
        <span>needs-human <b style={{ color: TERM.warn }}>{human}</b></span>
        <span>blocked <b style={{ color: TERM.fail }}>{blocked}</b></span>
        <span>stuck <b style={{ color: TERM.fail }}>{stuck}</b></span>
        <span>auto <b style={{ color: TERM.ok }}>{fmtRate(auto, 0)}</b></span>
        <span>cost <b style={{ color: TERM.accent }}>{fmtUsd(costTotal)}</b></span>
        {waste != null && <span>waste <b style={{ color: TERM.fail }}>{fmtUsd(waste)}</b></span>}
      </div>

      <SectionBar label="阶段总表" right="点击行可钻到对应节点" />
      <div style={{ border: `1px solid ${TERM.border}`, borderRadius: 8, overflow: 'hidden', background: '#fff' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' }}>
          <thead style={{ background: TERM.headBg }}>
            <tr>
              <th style={{ ...th, width: '16%' }}>STAGE</th>
              <th style={{ ...th, width: '9%' }}>COUNT</th>
              <th style={{ ...th, width: '12%' }}>SOURCE</th>
              <th style={{ ...th, width: '21%' }}>STATUS</th>
              <th style={{ ...th, width: '42%' }}>NOTES</th>
            </tr>
          </thead>
          <tbody>
            {stages.map((k, idx) => {
              const n = counts[k] ?? 0
              const node = NODES.find((x) => x.key === k)
              const note =
                k === 'merged'
                  ? '真正闭环'
                  : k === 'needs_human'
                    ? '当前最大卡点'
                    : node?.source === 'derived'
                      ? node.derivedFrom
                      : node?.desc
              return (
                <tr
                  key={k}
                  onClick={() => onNodeClick?.(k)}
                  style={{ background: idx % 2 ? TERM.rowAlt : '#fff', cursor: onNodeClick ? 'pointer' : 'default' }}
                >
                  <td style={{ ...td, color: TERM.header, fontWeight: 700 }}>{k}</td>
                  <td style={{ ...td, color: stageColor(k, n), fontWeight: 700 }}>{String(n).padStart(2, '0')}</td>
                  <td style={{ ...td, color: TERM.muted }}>{node?.source ?? 'real'}</td>
                  <td style={{ ...td }}>
                    {k === 'merged' && <span style={{ color: TERM.ok }}>OK</span>}
                    {k === 'needs_human' && n > 0 && <span style={{ color: TERM.warn }}>WAIT HUMAN</span>}
                    {k === 'blocked' && n > 0 && <span style={{ color: TERM.fail }}>BLOCKED</span>}
                    {k !== 'merged' && k !== 'needs_human' && k !== 'blocked' && (
                      <span style={{ color: n > 0 ? TERM.data : TERM.muted }}>{n > 0 ? 'ACTIVE' : 'IDLE'}</span>
                    )}
                  </td>
                  <td style={{ ...td, overflow: 'hidden', textOverflow: 'ellipsis' }}>{note}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <SectionBar label="滞留 / 风险任务" right="按卡点排序" />
      <div style={{ border: `1px solid ${TERM.border}`, borderRadius: 8, overflow: 'hidden', background: '#fff' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' }}>
          <thead style={{ background: TERM.headBg }}>
            <tr>
              <th style={{ ...th, width: '22%' }}>TASK</th>
              <th style={{ ...th, width: '20%' }}>STATE</th>
              <th style={{ ...th, width: '10%' }}>AGE</th>
              <th style={{ ...th, width: '10%' }}>RND</th>
              <th style={{ ...th, width: '12%' }}>COST</th>
              <th style={{ ...th, width: '26%' }}>PREVIEW</th>
            </tr>
          </thead>
          <tbody>
            {(analytics?.stuck ?? []).slice(0, 6).map((t: any, idx: number) => (
              <tr
                key={t.task_id ?? idx}
                onClick={() => onTaskClick?.(t)}
                style={{ background: idx % 2 ? TERM.rowAlt : '#fff', cursor: onTaskClick ? 'pointer' : 'default' }}
              >
                <td style={{ ...td, fontWeight: 700, color: TERM.data }}>{t.task_id}</td>
                <td style={{ ...td, color: TERM.fail }}>{t.state}</td>
                <td style={{ ...td, color: TERM.accent }}>{t.stuck_days?.toFixed?.(1) ?? '—'}d</td>
                <td style={{ ...td }}>{t.attempts ?? '—'}</td>
                <td style={{ ...td, color: TERM.accent }}>{t.cost_usd != null ? fmtUsd(t.cost_usd) : '—'}</td>
                <td style={{ ...td, overflow: 'hidden', textOverflow: 'ellipsis' }}>{t.preview}</td>
              </tr>
            ))}
            {!analytics?.stuck?.length && (
              <tr>
                <td colSpan={6} style={{ ...td, color: TERM.muted }}>no stuck tasks</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <SectionBar label="判据 / 成本" right="一眼看哪道闸门白占坑" />
      <div style={{ display: 'grid', gridTemplateColumns: '1.3fr 0.7fr', gap: 12 }}>
        <div style={{ border: `1px solid ${TERM.border}`, borderRadius: 8, overflow: 'hidden', background: '#fff' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' }}>
            <thead style={{ background: TERM.headBg }}>
              <tr>
                <th style={{ ...th, width: '34%' }}>GATE</th>
                <th style={{ ...th, width: '12%' }}>FIRED</th>
                <th style={{ ...th, width: '12%' }}>TP</th>
                <th style={{ ...th, width: '12%' }}>FP</th>
                <th style={{ ...th, width: '15%' }}>PREC</th>
                <th style={{ ...th, width: '15%' }}>VERDICT</th>
              </tr>
            </thead>
            <tbody>
              {(analytics?.gates?.rows ?? []).slice(0, 8).map((g: any, idx: number) => (
                <tr key={g.gate_id ?? idx} style={{ background: idx % 2 ? TERM.rowAlt : '#fff' }}>
                  <td style={{ ...td, fontWeight: 700 }}>{g.gate_id}</td>
                  <td style={{ ...td, color: TERM.accent }}>{g.fired}</td>
                  <td style={{ ...td, color: TERM.ok }}>{g.true_positives}</td>
                  <td style={{ ...td, color: TERM.fail }}>{g.false_positives}</td>
                  <td style={{ ...td }}>{g.precision == null ? '—' : fmtRate(g.precision, 0)}</td>
                  <td style={{ ...td, color: g.fired === 0 ? TERM.warn : TERM.data, overflow: 'hidden', textOverflow: 'ellipsis' }}>{g.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div style={{ border: `1px solid ${TERM.border}`, borderRadius: 8, background: '#fff', padding: 10 }}>
          <div style={{ color: TERM.header, fontWeight: 700, marginBottom: 6 }}>COST</div>
          <div style={{ display: 'grid', gap: 6, fontSize: 12, lineHeight: 1.6 }}>
            <div><span style={{ color: TERM.muted }}>total</span> <b style={{ color: TERM.accent }}>{fmtUsd(costTotal)}</b></div>
            <div><span style={{ color: TERM.muted }}>wasted</span> <b style={{ color: TERM.fail }}>{waste == null ? '—' : fmtUsd(waste)}</b></div>
            <div><span style={{ color: TERM.muted }}>per task</span> <b style={{ color: TERM.accent }}>{analytics?.cost?.per_task_usd == null ? '—' : fmtUsd(analytics.cost.per_task_usd)}</b></div>
            <div><span style={{ color: TERM.muted }}>tokens in</span> <b style={{ color: TERM.data }}>{analytics?.cost?.tokens_in ?? '—'}</b></div>
            <div><span style={{ color: TERM.muted }}>tokens out</span> <b style={{ color: TERM.data }}>{analytics?.cost?.tokens_out ?? '—'}</b></div>
            <div style={{ marginTop: 4, color: TERM.muted }}>
              终端视图的目的不是美化，而是让你一眼定位：<br />
              哪个阶段堆了、哪条判据白跑、钱烧在哪个模型上。
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
