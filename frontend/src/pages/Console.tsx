/**
 * 作战看板：一屏看完整个工厂在干什么。
 *
 * 为什么不复用 TaskList + Stats：那两页是「查东西」用的，一次回答一个问题，
 * 靠翻页串起来。演示和值班要的是反的 —— 不翻页、不点击，抬头就知道现在
 * 是否健康。所以这一页只做一件事：把五个桶、监工命中率、花费、卡住的任务
 * 塞进同一个视野，用颜色区分状态。
 *
 * 布局参考 k9s/lazydocker：等宽字体、青色表头、高对比度、底部快捷键栏。
 * 浅色而不是暗色 —— 投影和会议室灯光下暗色底看不清。
 */
import axios from 'axios'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import usePolling from '../hooks/usePolling'
import type { QueueState, Stats, TaskBuckets, TaskSummary } from '../types'

const POLL_MS = 10_000

/** /api/supervisors 的形状，见 factory/api.py:supervisor_stats。 */
interface RoleRow {
  role: string
  fired: number
  passed: number
  true_positives: number
  false_positives: number
  unadjudicated: number
  false_negatives: number
  faults: number
  harness_faults: number
  cost_usd: number
  tokens: number
  /** fired=0 时是 null（「不知道」），不是 0（「每次都误报」）。 */
  precision: number | null
}

interface GateRow {
  role: string
  gate: string
  fired: number
  true_positives: number
  false_positives: number
  unadjudicated: number
  precision: number | null
}

interface Supervisors {
  roles: RoleRow[]
  gates: GateRow[]
}

/** 五个桶的显示顺序 = 任务流动方向。乱序会让人以为流程是别的样子。 */
const FLOW: { key: QueueState; label: string; tone: string; bar: string }[] = [
  { key: 'inbox', label: 'INBOX', tone: 'text-slate-600', bar: 'bg-slate-400' },
  { key: 'running', label: 'RUNNING', tone: 'text-sky-700', bar: 'bg-sky-500' },
  { key: 'done', label: 'DONE', tone: 'text-emerald-700', bar: 'bg-emerald-500' },
  { key: 'needs_human', label: 'NEEDS-HUMAN', tone: 'text-amber-700', bar: 'bg-amber-500' },
  { key: 'blocked', label: 'BLOCKED', tone: 'text-rose-700', bar: 'bg-rose-500' },
]

/** null → n/a。0% 和「没数据」是相反的结论，混在一起会导致反向裁剪。 */
function pct(v: number | null): string {
  return v === null ? ' n/a' : `${(v * 100).toFixed(0)}%`.padStart(4)
}

function money(v: number): string {
  return `$${v.toFixed(2)}`
}


/** 相对时间。演示时「3 分钟前」比 ISO 时间戳有用得多。 */
function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.floor(s)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

/** 区块外框。ASCII 风格的标题栏 + 单像素边。 */
function Panel({
  title,
  right,
  children,
}: {
  title: string
  right?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="border border-slate-300 bg-white">
      <div className="flex items-center justify-between border-b border-slate-300 bg-slate-100 px-3 py-1">
        <h2 className="text-[11px] font-bold uppercase tracking-widest text-cyan-800">
          {title}
        </h2>
        {right ? <div className="text-[11px] text-slate-500">{right}</div> : null}
      </div>
      <div className="px-3 py-2">{children}</div>
    </section>
  )
}

/**
 * 顶栏：健康灯 + 三个总量 + 数据新鲜度。
 *
 * 新鲜度必须显示：轮询失败时 usePolling 保留上一次数据（这是对的，屏幕
 * 不该闪空），但屏幕上就会挂着一份过期数字看起来一切正常。所以把「几秒前
 * 更新」和错误横幅都放在最显眼的位置。
 */
function TopBar({
  stats,
  lastUpdated,
  error,
  onRefresh,
}: {
  stats: Stats | null
  lastUpdated: Date | null
  error: string | null
  onRefresh: () => void
}) {
  const blocked = stats?.by_state?.blocked ?? 0
  const needs = stats?.by_state?.needs_human ?? 0
  // 健康灯只看「有没有需要人介入的东西」。跑批失败不算不健康 —— 那正是
  // 这套系统预期会发生并且自己处理掉的事。
  const lamp =
    error !== null
      ? { c: 'bg-rose-500', t: 'API 不可达' }
      : (blocked ?? 0) > 0
        ? { c: 'bg-rose-500', t: `${blocked} 个硬闸门拦停` }
        : (needs ?? 0) > 0
          ? { c: 'bg-amber-500', t: `${needs} 个等人处理` }
          : { c: 'bg-emerald-500', t: '无人值守运行中' }

  return (
    <header className="border border-slate-300 bg-white">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${lamp.c}`} />
          <span className="text-sm font-bold tracking-tight">FACTORY</span>
          <span className="text-xs text-slate-500">{lamp.t}</span>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs">
          <span>
            <span className="text-slate-500">tasks </span>
            <span className="font-bold text-slate-900">{stats?.total_tasks ?? '—'}</span>
          </span>
          <span>
            <span className="text-slate-500">attempts </span>
            <span className="font-bold text-slate-900">{stats?.total_attempts ?? '—'}</span>
          </span>
          <span>
            <span className="text-slate-500">spend </span>
            <span className="font-bold text-amber-700">
              {stats ? money(stats.total_cost_usd) : '—'}
            </span>
          </span>
          <span>
            <span className="text-slate-500">avg/task </span>
            <span className="font-bold text-slate-900">
              {stats ? money(stats.avg_cost_per_task_usd) : '—'}
            </span>
          </span>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          className="ml-auto border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
        >
          [r] 刷新 · {lastUpdated ? `${ago(lastUpdated.toISOString())} 前` : '未更新'}
        </button>
      </div>
      {error !== null ? (
        <div className="border-t border-rose-300 bg-rose-50 px-3 py-1 text-[11px] text-rose-800">
          轮询失败：{error} —— 下面显示的是最后一次成功拉到的数据，可能已过期
        </div>
      ) : null}
    </header>
  )
}

/**
 * 流水线：五个桶横排 + 条形图。
 *
 * 条形按最大桶归一化而不是按总数：演示时 done=4 / inbox=1 这种量级，
 * 按总数算每根条都只有几个像素宽，等于没画。
 */
function Pipeline({ buckets }: { buckets: TaskBuckets | null }) {
  const counts = FLOW.map((f) => buckets?.[f.key]?.length ?? 0)
  const max = Math.max(1, ...counts)
  return (
    <Panel title="pipeline" right="任务流动方向 →">
      <div className="grid grid-cols-5 gap-3">
        {FLOW.map((f, i) => (
          <div key={f.key} className="min-w-0">
            <div className="flex items-baseline justify-between">
              <span className={`text-[10px] font-bold tracking-wider ${f.tone}`}>
                {f.label}
              </span>
              <span className="text-base font-bold leading-none text-slate-900">
                {counts[i]}
              </span>
            </div>
            <div className="mt-1 h-1.5 w-full bg-slate-100">
              <div
                className={`h-full ${f.bar}`}
                style={{ width: `${(counts[i] / max) * 100}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/**
 * 监工表：命中率 + 触发次数 + 未定案。
 *
 * unadjudicated 单独一列并且标黄：它是「这个数字还不能用来做裁剪决定」的
 * 唯一信号。混进 false_positives 里的话，一个从没被人核过的监工看起来
 * 就像误报了一堆。
 */
function Supervisors({ data }: { data: Supervisors | null }) {
  const rows = data?.roles ?? []
  return (
    <Panel title="supervisors · 按角色" right="precision = TP/(TP+FP)">
      {rows.length === 0 ? (
        <p className="text-xs text-slate-500">还没有监工裁决记录</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-wider text-cyan-800">
              <th className="pb-1 font-bold">role</th>
              <th className="pb-1 text-right font-bold">fired</th>
              <th className="pb-1 text-right font-bold">pass</th>
              <th className="pb-1 text-right font-bold">TP</th>
              <th className="pb-1 text-right font-bold">FP</th>
              <th className="pb-1 text-right font-bold">未定案</th>
              <th className="pb-1 text-right font-bold">precision</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map((r) => (
              <tr key={r.role} className="border-t border-slate-100">
                <td className="py-0.5 font-semibold text-slate-800">{r.role}</td>
                <td className="py-0.5 text-right text-slate-900">{r.fired}</td>
                <td className="py-0.5 text-right text-slate-400">{r.passed}</td>
                <td className="py-0.5 text-right text-emerald-700">{r.true_positives}</td>
                {/* 0 不标红：FP=0 是好消息，染成告警色会让人以为出了问题。
                    只有真的有误报时才需要抓眼。 */}
                <td
                  className={`py-0.5 text-right ${
                    r.false_positives > 0 ? 'font-bold text-rose-700' : 'text-slate-300'
                  }`}
                >
                  {r.false_positives}
                </td>
                <td
                  className={`py-0.5 text-right ${
                    r.unadjudicated > 0 ? 'bg-amber-50 font-bold text-amber-800' : 'text-slate-400'
                  }`}
                >
                  {r.unadjudicated}
                </td>
                <td
                  className={`py-0.5 text-right font-bold ${
                    r.precision === null ? 'text-slate-400' : 'text-slate-900'
                  }`}
                >
                  {pct(r.precision)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {rows.some((r) => r.precision === null) ? (
        <p className="mt-2 text-[11px] text-slate-500">
          n/a = 一次都没触发过。这不是 0% —— 照 0% 裁剪会删掉一个从没误报的监工
        </p>
      ) : null}
    </Panel>
  )
}

/**
 * 闸门表：按具体判据看，不按 role。
 *
 * 为什么必须分开：13 道硬闸门全报 role='risk'，按 role 聚合等于把 13 件事
 * 的命中率搅成一个数。一道从不触发的闸门（写错了判据、或者它防的那类问题
 * 根本不存在）在 role 表上完全看不出来 —— 它的 0 次触发被另外 12 道的命中
 * 稀释掉了。改进决策要的是「哪一道该删、哪一道该修」。
 */
function Gates({ data }: { data: Supervisors | null }) {
  const rows = data?.gates ?? []
  const max = Math.max(1, ...rows.map((g) => g.fired))
  return (
    <Panel title="gates · 按判据" right={`${rows.length} 道`}>
      {rows.length === 0 ? (
        <p className="text-xs text-slate-500">还没有闸门触发记录</p>
      ) : (
        <ul className="space-y-0.5">
          {rows.map((g) => (
            <li key={`${g.role}:${g.gate}`} className="flex items-center gap-2 text-xs">
              <span className="w-20 shrink-0 truncate text-[10px] uppercase tracking-wider text-slate-500">
                {g.role}
              </span>
              <span className="w-44 shrink-0 truncate font-mono text-slate-800">{g.gate}</span>
              <span className="h-1.5 w-24 shrink-0 bg-slate-100">
                <span
                  className="block h-full bg-cyan-600"
                  style={{ width: `${(g.fired / max) * 100}%` }}
                />
              </span>
              <span className="w-8 shrink-0 text-right font-mono text-slate-900">{g.fired}</span>
              <span className="w-10 shrink-0 text-right font-mono text-emerald-700">
                {g.true_positives > 0 ? `+${g.true_positives}` : ''}
              </span>
              <span
                className={`w-12 shrink-0 text-right font-mono ${
                  g.unadjudicated > 0 ? 'bg-amber-400 font-bold text-amber-950' : 'text-slate-300'
                }`}
              >
                {g.unadjudicated > 0 ? `?${g.unadjudicated}` : ''}
              </span>
              <span
                className={`w-10 shrink-0 text-right font-mono font-bold ${
                  g.precision === null ? 'text-slate-400' : 'text-slate-900'
                }`}
              >
                {pct(g.precision)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

/**
 * 收件箱：需要人介入的任务。
 *
 * 这一块默认展开并且给序号 —— 它是这个屏幕上唯一「需要人现在做点什么」
 * 的区域，其他面板都是回顾。序号和 `factory inbox` 打出来的一致，演示时
 * 可以直接说「第 2 个我现在定案」然后敲 `factory resolve 2 merged`。
 *
 * 空的时候要明确说「没有」而不是留白：空白分不出「没有」和「加载失败」。
 */
function Inbox({ buckets }: { buckets: TaskBuckets | null }) {
  // needs_human 和 blocked 合并显示：对人来说这两个都是「轮到我了」，
  // 差别只在原因。用左边一道色条区分，不用两张表 —— 两张表会让人以为
  // 要分两次处理。
  const rows: (TaskSummary & { state: QueueState })[] = [
    ...(buckets?.needs_human ?? []).map((t) => ({ ...t, state: 'needs_human' as QueueState })),
    ...(buckets?.blocked ?? []).map((t) => ({ ...t, state: 'blocked' as QueueState })),
  ]
  return (
    <Panel title="需要人介入" right={rows.length > 0 ? `${rows.length} 个` : undefined}>
      {rows.length === 0 ? (
        <p className="text-xs text-emerald-700">没有等人处理的任务</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((t, i) => (
            <li key={t.task_id} className="flex gap-2">
              <span className="w-4 shrink-0 pt-0.5 text-right font-mono text-[11px] text-slate-400">
                {i + 1}
              </span>
              <span
                className={`w-0.5 shrink-0 ${
                  t.state === 'blocked' ? 'bg-rose-500' : 'bg-amber-500'
                }`}
              />
              <div className="min-w-0 flex-1">
                <Link
                  to={`/task/${encodeURIComponent(t.task_id)}`}
                  className="block truncate font-mono text-xs font-semibold text-cyan-800 hover:underline"
                >
                  {t.task_id}
                </Link>
                <p className="truncate text-[11px] text-slate-500">{t.prompt_preview}</p>
                <p className="mt-0.5 flex gap-3 text-[10px] text-slate-500">
                  <span>{ago(t.mtime)} 前</span>
                  {t.resolution ? <span className="text-amber-700">{t.resolution}</span> : null}
                  {t.attempts_count ? <span>{t.attempts_count} 轮</span> : null}
                  {t.total_cost_usd !== undefined ? <span>{money(t.total_cost_usd)}</span> : null}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

/** resolution → 配色。和 Stats 页保持一致，两个页面并排看时不会打架。 */
const RESOLUTION_COLOR: Record<string, string> = {
  merged: 'bg-emerald-500',
  reworked: 'bg-amber-500',
  escalated: 'bg-orange-500',
  blocked: 'bg-rose-500',
  pending: 'bg-slate-400',
  not_dispatched: 'bg-slate-300',
}

/**
 * 定案分布：一根堆叠条。
 *
 * 用堆叠条而不是饼图：这几个数的意义是「占了多少比例」，而演示时最关心的
 * 是 pending 那一段有多宽 —— 堆叠条能一眼比出来，饼图得对着图例数。
 * pending 单独标出来的理由和监工表的 unadjudicated 一样：它是「这批数字
 * 还不能拿来做决定」的信号。
 */
function Resolutions({ stats }: { stats: Stats | null }) {
  const entries = Object.entries(stats?.by_resolution ?? {}).sort((a, b) => b[1] - a[1])
  const total = entries.reduce((s, [, v]) => s + v, 0)
  const pending = stats?.by_resolution?.pending ?? 0
  return (
    <Panel
      title="定案分布"
      right={total > 0 ? `${stats?.total_attempts ?? total} 轮` : undefined}
    >
      {total === 0 ? (
        <p className="text-xs text-slate-500">还没有跑过任何一轮</p>
      ) : (
        <>
          <div className="flex h-3 w-full overflow-hidden bg-slate-100">
            {entries.map(([k, v]) => (
              <div
                key={k}
                className={RESOLUTION_COLOR[k] ?? 'bg-slate-300'}
                style={{ width: `${(v / total) * 100}%` }}
                title={`${k}: ${v}`}
              />
            ))}
          </div>
          <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
            {entries.map(([k, v]) => (
              <li key={k} className="flex items-center gap-1.5">
                <span
                  className={`inline-block h-2 w-2 ${RESOLUTION_COLOR[k] ?? 'bg-slate-300'}`}
                />
                <span className="font-mono text-slate-700">{k}</span>
                <span className="font-mono font-bold text-slate-900">{v}</span>
              </li>
            ))}
          </ul>
          {pending > 0 ? (
            <p className="mt-2 text-[11px] text-amber-800">
              {pending} 轮还没定案 —— 命中率的分子建在这上面，
              <span className="font-mono"> factory resolve N merged</span> 可以清掉
            </p>
          ) : null}
        </>
      )}
    </Panel>
  )
}

/**
 * 页面本体。
 *
 * 三个端点各自轮询，不合并成一个「拉全部」的请求：其中任何一个 500
 * 不该让整屏变空。usePolling 保留上次数据 + 顶栏显示新鲜度，所以局部
 * 失败的表现是「那一块停在旧值」而不是白屏。
 */
export default function Console() {
  const fetchStats = useCallback(
    () => axios.get<Stats>('/api/stats').then((r) => r.data),
    [],
  )
  const fetchTasks = useCallback(
    () => axios.get<TaskBuckets>('/api/tasks').then((r) => r.data),
    [],
  )
  const fetchSup = useCallback(
    () => axios.get<Supervisors>('/api/supervisors').then((r) => r.data),
    [],
  )

  const stats = usePolling(fetchStats, POLL_MS)
  const tasks = usePolling(fetchTasks, POLL_MS)
  const sup = usePolling(fetchSup, POLL_MS)

  const refreshAll = () => {
    stats.refresh()
    tasks.refresh()
    sup.refresh()
  }

  // 首屏还没有任何数据时给一行提示。三份数据都可能是 null，但只要有一份
  // 到了就开始渲染 —— 让屏幕分块出现比整屏等最慢的那个请求好。
  const bootstrapping =
    stats.data === null && tasks.data === null && sup.data === null && stats.error === null

  return (
    // 整页等宽：这是看板不是文章，列对齐比易读性优先。数字在等宽字体下
    // 才能不用 padding 就对齐，扫一眼就能比大小。
    <div className="space-y-2 font-mono [font-variant-numeric:tabular-nums]">
      <TopBar
        stats={stats.data}
        lastUpdated={stats.lastUpdated}
        error={stats.error ?? tasks.error ?? sup.error}
        onRefresh={refreshAll}
      />

      {bootstrapping ? (
        <p className="border border-slate-300 bg-white px-3 py-6 text-center text-xs text-slate-500">
          正在连接 /api …
        </p>
      ) : null}

      <Pipeline buckets={tasks.data} />

      <div className="grid gap-2 lg:grid-cols-2">
        <div className="space-y-2">
          <Supervisors data={sup.data} />
          <Resolutions stats={stats.data} />
        </div>
        <div className="space-y-2">
          <Inbox buckets={tasks.data} />
          <Gates data={sup.data} />
        </div>
      </div>

      {/* 底部快捷键栏：k9s 的做法。这里列的是 CLI 命令而不是页面快捷键 ——
          屏幕上看到问题之后，下一步动作发生在终端里。 */}
      <footer className="flex flex-wrap items-center gap-x-4 gap-y-1 border border-slate-300 bg-slate-100 px-3 py-1 text-[11px]">
        {[
          ['factory inbox', '看谁卡住'],
          ['factory resolve N merged --why …', '人工定案'],
          ['factory loop --idle watch', '起跑批'],
        ].map(([cmd, what]) => (
          <span key={cmd}>
            <span className="bg-cyan-800 px-1 font-bold text-white">{cmd}</span>
            <span className="ml-1.5 text-slate-600">{what}</span>
          </span>
        ))}
        <span className="ml-auto text-slate-400">每 {POLL_MS / 1000}s 自动刷新</span>
      </footer>
    </div>
  )
}
