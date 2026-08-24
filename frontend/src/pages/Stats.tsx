import axios from 'axios'
import { useCallback } from 'react'
import usePolling from '../hooks/usePolling'
import StatCard from '../components/StatCard'
import type { QueueState, Stats as StatsData } from '../types'

// 统计页：四张大数字卡 + 两个纯 CSS 分布条。零图表库 —— 分布条就是
// 一个 div 套一个 div，宽度 width: `${pct}%`，比引 echarts 省 300KB。

const POLL_MS = 60_000

const currency = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
})

const integer = new Intl.NumberFormat('en-US')

/** resolution / state → Tailwind bg 类。与 T03/T04 的徽章配色保持一致。 */
const RESOLUTION_COLOR: Record<string, string> = {
  merged: 'bg-green-400',
  reworked: 'bg-yellow-400',
  escalated: 'bg-orange-400',
  blocked: 'bg-red-400',
  not_dispatched: 'bg-slate-300',
}

const STATE_COLOR: Record<QueueState, string> = {
  inbox: 'bg-slate-300',
  running: 'bg-blue-400',
  done: 'bg-green-400',
  needs_human: 'bg-orange-400',
  blocked: 'bg-red-400',
}

/** 队列桶的固定顺序 + 中文标签。五个桶恒定存在，缺的显示 0 / —。 */
const STATE_ROWS: { key: QueueState; label: string }[] = [
  { key: 'inbox', label: 'inbox' },
  { key: 'running', label: 'running' },
  { key: 'done', label: 'done' },
  { key: 'needs_human', label: 'needs-human' },
  { key: 'blocked', label: 'blocked' },
]

/** resolution 的展示顺序。API 里没出现的键不显示（不是 0，是压根没有）。 */
const RESOLUTION_ORDER = [
  'merged',
  'reworked',
  'escalated',
  'blocked',
  'not_dispatched',
]

interface BarRow {
  key: string
  label: string
  /** null = 这个数拿不到（后端 QueueUnreadable），渲染成 — 且不画条 */
  value: number | null
  color: string
}

/**
 * 纯 CSS 横向分布条。
 *
 * 除零保护：分母用「非 null 值之和」，为 0 时整块显示「暂无数据」而不是
 * 画一排 NaN% 宽的条。全是 null 时也走这条路，但文案不同 —— 「读不到」
 * 和「没有数据」是两件事，混成一句会让人以为队列是空的。
 */
function DistributionBar({
  rows,
  unknownText,
}: {
  rows: BarRow[]
  /** rows 非空但全部为 null 时显示的文案（「读不到」，区别于「没有数据」） */
  unknownText: string
}) {
  const known = rows.filter((r): r is BarRow & { value: number } =>
    r.value !== null,
  )
  const total = known.reduce((sum, r) => sum + r.value, 0)

  // 一行都没有 → 确实没数据。注意这跟「有行但值全是 null」不是一回事：
  // 后者是读不到，前者是压根没产生过。两种情况文案必须不同。
  if (rows.length === 0) {
    return <p className="py-6 text-sm text-slate-400">暂无数据</p>
  }

  if (known.length === 0) {
    return <p className="py-6 text-sm text-slate-400">{unknownText}</p>
  }

  if (total === 0) {
    return <p className="py-6 text-sm text-slate-400">暂无数据</p>
  }

  return (
    <ul className="space-y-2">
      {rows.map((row) => {
        // value 为 null 时不画条；为 0 时画一条 0 宽（即什么都看不到），
        // 但右侧仍显示 0 —— 「这个桶是空的」是有效信息。
        const pct = row.value === null ? 0 : (row.value / total) * 100
        return (
          <li key={row.key} className="flex items-center gap-3">
            <span className="w-28 shrink-0 font-mono text-xs text-slate-600">
              {row.label}
            </span>
            <span className="h-4 flex-1 overflow-hidden rounded bg-slate-100">
              {row.value === null ? null : (
                <span
                  className={`block h-full rounded ${row.color}`}
                  style={{ width: `${pct}%` }}
                />
              )}
            </span>
            <span className="w-16 shrink-0 text-right text-sm tabular-nums text-slate-700">
              {row.value === null ? '—' : integer.format(row.value)}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

async function fetchStats(): Promise<StatsData> {
  const res = await axios.get<StatsData>('/api/stats')
  return res.data
}

export default function Stats() {
  const fetcher = useCallback(fetchStats, [])
  const { data, error, lastUpdated, loading } = usePolling<StatsData>(
    fetcher,
    POLL_MS,
  )

  // 首屏还没数据时才显示 loading；之后拉取失败保留旧数据，只加一条警告条。
  if (!data) {
    if (loading) {
      return <p className="p-6 text-sm text-slate-400">加载中…</p>
    }
    return (
      <div className="p-6">
        <p className="text-sm text-red-600">
          统计数据加载失败{error ? `：${error}` : ''}
        </p>
      </div>
    )
  }

  const byState = data.by_state
  const merged = data.by_resolution.merged ?? 0

  const resolutionRows: BarRow[] = RESOLUTION_ORDER
    // API 里没出现的 resolution 直接不显示：它不是 0，是这套系统还没产生过
    // 这种结局。补一行 0 会让人以为统计过了。
    .filter((key) => key in data.by_resolution)
    .map((key) => ({
      key,
      label: key.replace(/_/g, '-'),
      value: data.by_resolution[key],
      color: RESOLUTION_COLOR[key] ?? 'bg-slate-300',
    }))

  const stateRows: BarRow[] = STATE_ROWS.map(({ key, label }) => ({
    key,
    label,
    // by_state 整个为 null，或某个桶为 null，都要落到 null 而不是 0
    value: byState === null ? null : (byState[key] ?? null),
    color: STATE_COLOR[key],
  }))

  return (
    <div className="mx-auto max-w-6xl p-6">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-bold text-slate-900">统计</h1>
        <span className="text-xs text-slate-400">
          {lastUpdated
            ? `上次更新 ${lastUpdated.toLocaleTimeString('zh-CN')} · 每 60 秒刷新`
            : '每 60 秒刷新'}
        </span>
      </header>

      {error ? (
        <div className="mb-6 rounded border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          刷新失败，下面是上次成功拉到的数据：{error}
        </div>
      ) : null}

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="总任务数"
          value={integer.format(data.total_tasks)}
          hint="审计库里跑过的任务；队列里未派发的不计入"
        />
        <StatCard
          label="总花费"
          value={currency.format(data.total_cost_usd)}
          hint={`${integer.format(data.total_attempts)} 轮尝试累计`}
          accent
        />
        <StatCard
          label="已合并"
          value={integer.format(merged)}
          hint="resolution 为 merged 的轮次"
        />
        <StatCard
          label="平均每任务"
          value={currency.format(data.avg_cost_per_task_usd)}
          hint={`平均 ${data.avg_attempts_per_task} 轮/任务`}
          accent
        />
      </section>

      <section className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="mb-4 text-sm font-semibold text-slate-700">
            按结局分布
          </h2>
          <DistributionBar
            rows={resolutionRows}
            unknownText="审计库当前读不到，这几个数暂时无法统计"
          />
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="mb-4 text-sm font-semibold text-slate-700">
            按队列状态
          </h2>
          <DistributionBar
            rows={stateRows}
            unknownText="队列当前读不到，这几个数暂时无法统计"
          />
        </div>
      </section>
    </div>
  )
}
