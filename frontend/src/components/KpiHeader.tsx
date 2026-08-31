import { Progress, Tag, Tooltip } from 'antd'
import { C } from '../theme/tokens'
import { fmtRate, fmtUsd } from '../theme/geometry'

/**
 * 深色渐变 KPI 头条 —— 移植自 OA 闭环监控页。
 *
 * 保留复刻件要点 #3「口径标注」：真实系统上「自动化率」出现两次，
 * 一处 63% 一处 83.1%，同名不同口径且都不标注，看的人无法判断谁对。
 * 这里每个环都强制带 scope 标签。
 */

export interface KpiRing {
  key: string
  title: string
  rate: number | null
  scope: string
  sub: string
  color: string
  weak?: boolean
}

export interface KpiCounter {
  key: string
  label: string
  value: string | number
  color: string
  icon: 'thunder' | 'close' | 'check' | 'clock'
}

const ICONS = { thunder: '⚡', close: '✕', check: '✓', clock: '◷' }

function Ring({ rate, title, sub, color, scope, weak }: Omit<KpiRing, 'key'>) {
  // rate 为 null（分母 0）时画空环 + 破折号，不画 0% —— 0% 会被读成「全失败」
  const pct = rate == null ? 0 : Math.round(rate * 100)
  return (
    <div style={{ textAlign: 'center', minWidth: 118 }}>
      <Progress
        type="circle"
        percent={pct}
        size={72}
        strokeColor={rate == null ? 'rgba(255,255,255,0.25)' : color}
        trailColor="rgba(255,255,255,0.16)"
        format={() => (
          <span style={{ color: '#fff', fontSize: 17, fontWeight: 600 }}>
            {rate == null ? '—' : `${pct}%`}
          </span>
        )}
      />
      <div
        style={{
          marginTop: 8,
          fontSize: 12,
          color: '#fff',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 4,
        }}
      >
        {title}
        <Tooltip title={`统计口径：${scope}`}>
          <Tag
            bordered={false}
            style={{
              margin: 0,
              fontSize: 10,
              lineHeight: '16px',
              padding: '0 4px',
              background: 'rgba(255,255,255,0.14)',
              color: 'rgba(255,255,255,0.85)',
              cursor: 'help',
            }}
          >
            {scope}
          </Tag>
        </Tooltip>
      </div>
      <div style={{ fontSize: 11, color: weak ? C.warning : 'rgba(255,255,255,0.55)', marginTop: 2 }}>
        {sub}
      </div>
    </div>
  )
}

interface Props {
  rings: KpiRing[]
  counters: KpiCounter[]
  costTotal: number
  costWasted: number | null
  insight: string
  caveat?: string
}

export default function KpiHeader({
  rings,
  counters,
  costTotal,
  costWasted,
  insight,
  caveat,
}: Props) {
  const wastedPct = costTotal > 0 && costWasted != null ? costWasted / costTotal : null
  return (
    <div
      style={{
        background: 'linear-gradient(135deg, #1E293B 0%, #0F172A 100%)',
        borderRadius: 12,
        padding: '20px 24px',
        color: '#fff',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 28, flexWrap: 'wrap' }}>
        {rings.map(({ key, ...r }) => (
          <Ring key={key} {...r} />
        ))}

        {/* 白烧钱占比 —— 单独一格，不做成环：它不是「完成度」类指标 */}
        <div style={{ textAlign: 'center', minWidth: 128, paddingTop: 6 }}>
          <div style={{ fontSize: 26, fontWeight: 600 }}>
            {fmtUsd(costWasted)}
            <span style={{ fontSize: 15, color: 'rgba(255,255,255,0.45)' }}>
              /{fmtUsd(costTotal)}
            </span>
          </div>
          <div style={{ fontSize: 12, marginTop: 10 }}>花在未合并任务上</div>
          <div style={{ fontSize: 11, color: C.warning, marginTop: 2 }}>
            {fmtRate(wastedPct, 0)}
          </div>
        </div>

        <div style={{ flex: 1 }} />

        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          {counters.map((c) => (
            <div
              key={c.key}
              style={{
                background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: 8,
                padding: '10px 16px',
                minWidth: 88,
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: 22, fontWeight: 600, color: c.color }}>
                <span style={{ fontSize: 13, marginRight: 4 }}>{ICONS[c.icon]}</span>
                {c.value}
              </div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.6)', marginTop: 2 }}>
                {c.label}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div
        style={{
          marginTop: 16,
          paddingTop: 12,
          borderTop: '1px solid rgba(255,255,255,0.08)',
          fontSize: 12,
          color: 'rgba(255,255,255,0.72)',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexWrap: 'wrap',
        }}
      >
        <span>💡 {insight}</span>
        {caveat && (
          <Tag bordered={false} color="warning" style={{ fontSize: 11, whiteSpace: 'normal' }}>
            {caveat}
          </Tag>
        )}
      </div>
    </div>
  )
}
