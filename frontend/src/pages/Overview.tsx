import axios from 'axios'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import ClosureRing from '../components/ClosureRing'
import usePolling from '../hooks/usePolling'
import type { Analytics, GateRow, Metric, TrendPoint } from '../types'

// 闭环总览页：一屏回答「这套东西现在健康吗、钱花在哪、哪道判据该修、谁卡住了」。
//
// 数据全部来自单个 /api/analytics。八个模块共用一次请求，所以不会出现各块
// 数据来自不同时刻的情况 —— 那是多端点看板最难查的一类 bug（两个数字对不上，
// 其实只是差了 3 秒）。
//
// 零图表库：柱状图是 div 高度，环形图是手写 SVG。引 echarts 要 300KB+，
// 而这一页需要的图形复杂度用不上它。

const POLL_MS = 30_000

const WINDOWS = [7, 14, 30, 90] as const

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
})

/** token 数动辄千万，完整数字读不出量级。 */
function tokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(n)
}

/** 百分比。null → —（算不出来，不是 0）。 */
function pct(v: number | null, digits = 1): string {
  return v === null ? '—' : `${(v * 100).toFixed(digits)}%`
}

/**
 * geek 风面板外框。和 Console 的 Panel 保持同一套：cyan 小标题 + 直角边框。
 * 刻意不抽到共享组件 —— 两页对 right 槽的用法已经分叉，强行合并会长出
 * 一堆布尔开关。
 */
function Panel({
  title,
  right,
  children,
  className = '',
}: {
  title: string
  right?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={`border border-slate-300 bg-white ${className}`}>
      <header className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-3 py-1.5">
        <h2 className="text-[11px] font-bold uppercase tracking-widest text-cyan-800">
          {title}
        </h2>
        {right && <span className="font-mono text-[10px] text-slate-500">{right}</span>}
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}

/**
 * 单个 KPI。
 *
 * 口径（basis）和分子分母是**必须显示**的，不是可选装饰。同一个「自动化率」
 * 在不同分母下能差一倍，只显示百分比的看板会让两个人对着同一屏读出不同结论。
 */
function MetricCell({ m }: { m: Metric }) {
  const isRatio = !m.unit
  const dim = m.value === null
  return (
    <div className="border border-slate-200 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">
        {m.label}
      </div>
      <div
        className={`mt-1 font-mono text-2xl font-bold ${
          dim ? 'text-slate-400' : 'text-slate-900'
        }`}
      >
        {isRatio ? pct(m.value) : m.value === null ? '—' : `${m.value}${m.unit}`}
      </div>
      <div className="mt-0.5 font-mono text-[10px] text-slate-600">
        {m.hit}/{m.of}
      </div>
      <div className="mt-1 text-[10px] leading-tight text-slate-400">{m.basis}</div>
    </div>
  )
}

/**
 * 纯 CSS 双序列柱状图（轮次 / 其中已合并）。
 *
 * 全 0 时不画 0 高的柱子然后配一根 0 刻度轴 —— 那看起来像图表坏了。直接一句
 * 「这个窗口内没有执行记录」，并把窗口天数说出来，因为「没数据」和「窗口选窄了」
 * 是两件事，用户要能自己分辨。
 */
function TrendBars({ points, windowDays }: { points: TrendPoint[]; windowDays: number }) {
  const max = Math.max(...points.map((p) => p.runs), 0)
  if (max === 0) {
    return (
      <p className="py-8 text-center text-xs text-slate-400">
        近 {windowDays} 天没有执行记录。换更长的窗口试试。
      </p>
    )
  }
  // 天数多时不可能每根都标日期，取首/中/末三个锚点。
  const labelAt = new Set([0, Math.floor(points.length / 2), points.length - 1])
  return (
    <div>
      <div className="flex h-32 items-end gap-[2px]">
        {points.map((p, i) => {
          const h = (p.runs / max) * 100
          const mh = p.runs > 0 ? (p.merged / p.runs) * h : 0
          return (
            <div
              key={p.date}
              className="group relative flex-1"
              style={{ minWidth: 3 }}
              title={`${p.date}\n轮次 ${p.runs} · 已合并 ${p.merged} · ${money.format(p.cost_usd)}`}
            >
              <div
                className="w-full bg-sky-200 transition-colors group-hover:bg-sky-300"
                style={{ height: `${h}%`, minHeight: p.runs > 0 ? 2 : 0 }}
              >
                {/* 已合并的部分叠在柱子底部，用深绿区分 */}
                <div
                  className="w-full bg-green-500"
                  style={{
                    height: `${h > 0 ? (mh / h) * 100 : 0}%`,
                    marginTop: `${h > 0 ? 100 - (mh / h) * 100 : 0}%`,
                  }}
                />
              </div>
              {labelAt.has(i) && (
                <div className="absolute -bottom-4 left-0 whitespace-nowrap font-mono text-[9px] text-slate-400">
                  {p.date.slice(5)}
                </div>
              )}
            </div>
          )
        })}
      </div>
      <div className="mt-5 flex gap-4 font-mono text-[10px] text-slate-500">
        <span className="flex items-center gap-1">
          <i className="inline-block h-2 w-2 bg-sky-200" /> 轮次 (峰值 {max})
        </span>
        <span className="flex items-center gap-1">
          <i className="inline-block h-2 w-2 bg-green-500" /> 其中已合并
        </span>
      </div>
    </div>
  )
}

/** 判据表的一行。precision 为 null 时显示「未定案」而不是 0%。 */
function GateTableRow({ g }: { g: GateRow }) {
  const never = g.fired === 0
  const bad = g.precision !== null && g.precision < 0.3
  return (
    <tr className={`border-t border-slate-100 ${never ? 'bg-slate-50' : ''}`}>
      <td className="py-1 pr-2 font-mono text-[11px] text-slate-700">{g.gate_id}</td>
      <td className="py-1 pr-2 text-right font-mono text-[11px]">
        {never ? <span className="text-slate-400">0</span> : g.fired}
      </td>
      <td className="py-1 pr-2 text-right font-mono text-[11px] text-green-700">
        {g.true_positives || '·'}
      </td>
      <td className="py-1 pr-2 text-right font-mono text-[11px] text-red-600">
        {g.false_positives || '·'}
      </td>
      <td className="py-1 pr-2 text-right font-mono text-[11px] text-amber-600">
        {g.unadjudicated || '·'}
      </td>
      <td
        className={`py-1 pr-2 text-right font-mono text-[11px] font-bold ${
          g.precision === null ? 'text-slate-400' : bad ? 'text-red-600' : 'text-slate-800'
        }`}
      >
        {g.precision === null ? '未定案' : pct(g.precision, 0)}
      </td>
      <td className="py-1 text-[10px] leading-tight text-slate-500">{g.verdict}</td>
    </tr>
  )
}

export default function Overview() {
  const [days, setDays] = useState<number>(30)

  const fetcher = useCallback(async () => {
    const r = await axios.get<Analytics>('/api/analytics', { params: { days } })
    return r.data
  }, [days])

  const { data, error, loading, lastUpdated, refresh } = usePolling(fetcher, POLL_MS)

  if (loading) {
    return <p className="p-6 font-mono text-sm text-slate-500">载入中…</p>
  }

  // 请求失败 ≠ 没有数据。这一页宁可整块报错，也不能把 500 渲染成空看板 ——
  // 那会让人得出「工厂从来没跑过」的结论。
  if (error && !data) {
    return (
      <div className="m-6 border border-red-300 bg-red-50 p-4">
        <p className="font-mono text-sm font-bold text-red-700">/api/analytics 拉取失败</p>
        <p className="mt-1 font-mono text-xs text-red-600">{error}</p>
        <p className="mt-2 text-xs text-slate-600">
          这不表示没有数据，只表示这一页现在读不到。检查 factory-api 服务：
          <code className="ml-1 bg-white px-1">systemctl status factory-api</code>
        </p>
        <button
          onClick={refresh}
          className="mt-3 border border-red-400 px-3 py-1 font-mono text-xs text-red-700 hover:bg-red-100"
        >
          重试
        </button>
      </div>
    )
  }

  if (!data) return null

  const closure = data.core.auto_resolved_rate

  return (
    <div className="space-y-3 p-4">
      {/* 顶部：窗口切换 + 刷新状态 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border border-slate-300 bg-slate-50 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-bold uppercase tracking-widest text-cyan-800">
            闭环总览
          </span>
          <span className="font-mono text-[10px] text-slate-500">
            窗口 {data.window_days}d · 生成于 {data.generated_at.replace('T', ' ')}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setDays(w)}
              className={`border px-2 py-0.5 font-mono text-[11px] ${
                w === days
                  ? 'border-cyan-600 bg-cyan-600 text-white'
                  : 'border-slate-300 bg-white text-slate-600 hover:border-cyan-500'
              }`}
            >
              {w}d
            </button>
          ))}
          <button
            onClick={refresh}
            className="ml-2 border border-slate-300 bg-white px-2 py-0.5 font-mono text-[11px] text-slate-600 hover:border-cyan-500"
          >
            ⟳
          </button>
          <span className="ml-1 font-mono text-[10px] text-slate-400">
            {lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
          </span>
        </div>
      </div>

      {/* 降级横幅：部分数据源挂了，逐条点名，不静默 */}
      {data.degraded.length > 0 && (
        <div className="border border-amber-300 bg-amber-50 px-3 py-2">
          <p className="font-mono text-[11px] font-bold text-amber-800">
            部分数据源不可用 —— 下面标 — 的格子是「读不到」，不是 0
          </p>
          <ul className="mt-1 list-inside list-disc font-mono text-[10px] text-amber-700">
            {data.degraded.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </div>
      )}

      {/* 轮询失败但仍有旧数据：显示陈旧警告，不清空页面 */}
      {error && data && (
        <div className="border border-amber-300 bg-amber-50 px-3 py-1.5 font-mono text-[11px] text-amber-800">
          最近一次刷新失败（{error}），显示的是 {lastUpdated?.toLocaleTimeString()} 的快照
        </div>
      )}

      {data.empty ? (
        <div className="border border-slate-300 bg-white p-8 text-center">
          <p className="font-mono text-sm text-slate-500">审计库里还没有任何执行记录</p>
          <p className="mt-1 text-xs text-slate-400">
            这是真的空（库读得到、就是没数据），不是读取失败。投一个任务后这一页就有内容。
          </p>
        </div>
      ) : (
        <>
          {/* KPI 行 */}
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
            {Object.entries(data.core).map(([k, m]) => (
              <MetricCell key={k} m={m} />
            ))}
          </div>

          {/* 环形图 + 成本 */}
          <div className="grid gap-3 lg:grid-cols-[1fr_360px]">
            <Panel title="闭环流转" right={`${data.stages.length} 阶段`}>
              <ClosureRing
                stages={data.stages}
                closureRate={closure.value}
                closureOf={closure.of}
                closureHit={closure.hit}
              />
            </Panel>

            <div className="space-y-3">
              <Panel title="成本" right={money.format(data.cost.total_usd)}>
                <dl className="space-y-1.5 font-mono text-[11px]">
                  <div className="flex justify-between">
                    <dt className="text-slate-500">每任务均摊</dt>
                    <dd className="font-bold">
                      {data.cost.per_task_usd === null
                        ? '—'
                        : money.format(data.cost.per_task_usd)}
                    </dd>
                  </div>
                  <div className="flex justify-between border-t border-slate-100 pt-1.5">
                    <dt className="text-slate-500">未合并任务耗费</dt>
                    <dd className="font-bold text-red-600">
                      {money.format(data.cost.wasted_usd)}
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">占总花费</dt>
                    <dd className="text-red-600">
                      {pct(
                        data.cost.total_usd > 0
                          ? data.cost.wasted_usd / data.cost.total_usd
                          : null,
                        0,
                      )}
                    </dd>
                  </div>
                  <div className="flex justify-between border-t border-slate-100 pt-1.5">
                    <dt className="text-slate-500">token in / out</dt>
                    <dd>
                      {tokens(data.cost.tokens_in)} / {tokens(data.cost.tokens_out)}
                    </dd>
                  </div>
                </dl>
                <p className="mt-2 border-t border-slate-100 pt-2 text-[10px] leading-relaxed text-slate-400">
                  「未合并耗费」= 末轮 resolution ≠ merged 的所有轮次花费之和。
                  它包含仍在进行中的任务，所以不等于纯浪费；但它是降本时第一个该看的数。
                </p>
              </Panel>

              <Panel title="模型花费构成" right={`${data.cost.by_model.length} 个`}>
                <table className="w-full">
                  <thead>
                    <tr className="text-[10px] uppercase tracking-wider text-cyan-800">
                      <th className="pb-1 text-left font-bold">模型</th>
                      <th className="pb-1 text-right font-bold">轮</th>
                      <th className="pb-1 text-right font-bold">花费</th>
                      <th className="pb-1 text-right font-bold">占比</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.cost.by_model.map((m) => (
                      <tr key={m.model} className="border-t border-slate-100">
                        <td className="py-1 font-mono text-[11px]">{m.model}</td>
                        <td className="py-1 text-right font-mono text-[11px]">{m.runs}</td>
                        <td className="py-1 text-right font-mono text-[11px]">
                          {money.format(m.cost_usd)}
                        </td>
                        <td className="py-1 text-right font-mono text-[11px] text-slate-500">
                          {pct(m.share, 0)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            </div>
          </div>

          {/* 趋势 + 卡点 */}
          <div className="grid gap-3 lg:grid-cols-[1fr_400px]">
            <Panel title="执行趋势" right={`近 ${data.window_days} 天`}>
              <TrendBars points={data.trend} windowDays={data.window_days} />
            </Panel>

            <Panel
              title="卡点任务"
              right={data.stuck.length > 0 ? `${data.stuck.length} 个待处理` : '无'}
            >
              {data.stuck.length === 0 ? (
                <p className="py-6 text-center text-xs text-slate-400">
                  没有任务卡在 needs-human / blocked
                </p>
              ) : (
                <ul className="space-y-2">
                  {data.stuck.map((t) => (
                    <li key={t.task_id} className="border border-slate-200 px-2 py-1.5">
                      <div className="flex items-baseline justify-between gap-2">
                        <Link
                          to={`/task/${encodeURIComponent(t.task_id)}`}
                          className="truncate font-mono text-[11px] font-bold text-cyan-700 hover:underline"
                        >
                          {t.task_id}
                        </Link>
                        <span
                          className={`shrink-0 px-1 font-mono text-[10px] ${
                            t.state === 'blocked'
                              ? 'bg-red-100 text-red-700'
                              : 'bg-orange-100 text-orange-700'
                          }`}
                        >
                          {t.state}
                        </span>
                      </div>
                      <p className="mt-0.5 truncate text-[10px] text-slate-500">
                        {t.preview}
                      </p>
                      <div className="mt-1 flex gap-3 font-mono text-[10px] text-slate-500">
                        <span
                          className={
                            (t.stuck_days ?? 0) > 3 ? 'font-bold text-red-600' : ''
                          }
                        >
                          滞留 {t.stuck_days === null ? '—' : `${t.stuck_days}d`}
                        </span>
                        <span>{t.attempts} 轮</span>
                        <span>{money.format(t.cost_usd)}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-2 border-t border-slate-100 pt-2 text-[10px] leading-relaxed text-slate-400">
                只列 needs-human 和 blocked。inbox 里排队是正常的，running 久是任务本身重
                —— 真正的卡点是「等人而人没来」。
              </p>
            </Panel>
          </div>

          {/* 判据表 */}
          <Panel
            title="判据闸门表现"
            right={`${data.gates.rows.length} 道 · ${data.gates.never_fired} 道从未触发`}
          >
            {/* 偏差声明放表格上方而不是脚注：它决定这张表能不能用来做决策 */}
            <div
              className={`mb-2 border px-2 py-1.5 text-[10px] leading-relaxed ${
                data.gates.caveat.reliable
                  ? 'border-slate-200 bg-slate-50 text-slate-600'
                  : 'border-amber-300 bg-amber-50 text-amber-800'
              }`}
            >
              <strong>口径提醒：</strong>
              {data.gates.caveat.text}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px]">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wider text-cyan-800">
                    <th className="pb-1 text-left font-bold">闸门</th>
                    <th className="pb-1 text-right font-bold">触发</th>
                    <th className="pb-1 text-right font-bold">真报</th>
                    <th className="pb-1 text-right font-bold">误报</th>
                    <th className="pb-1 text-right font-bold">待定案</th>
                    <th className="pb-1 text-right font-bold">命中率</th>
                    <th className="pb-1 text-left font-bold">建议</th>
                  </tr>
                </thead>
                <tbody>
                  {data.gates.rows.map((g) => (
                    <GateTableRow key={g.gate_id} g={g} />
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}
    </div>
  )
}
