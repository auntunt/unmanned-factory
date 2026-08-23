import { useCallback, useEffect } from 'react'
import axios from 'axios'
import type { QueueState, TaskBuckets, TaskSummary } from '../types'
import { usePolling } from '../hooks/usePolling'
import TaskCard from '../components/TaskCard'
import {
  STATE_DOT_CLASSES,
  STATE_ICONS,
  STATE_LABELS,
} from '../components/StateBadge'
import { KeyBar, StatusBar } from '../components/ConsoleChrome'

const POLL_INTERVAL_MS = 60_000

// 看板列顺序 = 任务在流水线上的推进顺序
const BUCKET_ORDER: QueueState[] = [
  'inbox',
  'running',
  'done',
  'needs_human',
  'blocked',
]

const EMPTY_BUCKETS: TaskBuckets = {
  inbox: [],
  running: [],
  done: [],
  needs_human: [],
  blocked: [],
}

async function fetchTasks(): Promise<TaskBuckets> {
  // 生产环境同源反代，开发环境 vite proxy —— 始终用相对路径
  const res = await axios.get<TaskBuckets>('/api/tasks')
  return res.data
}

/** 后端保证五桶恒在，但仍做一次兜底，避免某桶缺失时整页崩掉 */
function normalize(data: TaskBuckets | null): TaskBuckets {
  if (!data) return EMPTY_BUCKETS
  const out: TaskBuckets = { ...EMPTY_BUCKETS }
  for (const state of BUCKET_ORDER) {
    const bucket: TaskSummary[] | undefined = data[state]
    out[state] = Array.isArray(bucket) ? bucket : []
  }
  return out
}

function formatClock(d: Date): string {
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

interface BucketColumnProps {
  state: QueueState
  tasks: TaskSummary[]
}

function BucketColumn({ state, tasks }: BucketColumnProps) {
  return (
    // 方角、单像素边框，标题条灰底——k9s 的分区表格
    <section className="flex min-w-0 flex-col border border-slate-300 bg-white md:min-h-[22rem]">
      <header className="flex items-baseline gap-1.5 border-b border-slate-300 bg-slate-100 px-2 py-1">
        <span
          className={`shrink-0 font-mono text-xs font-bold ${STATE_DOT_CLASSES[state]}`}
          aria-hidden="true"
        >
          {STATE_ICONS[state]}
        </span>
        <h2 className="font-mono text-xs font-semibold uppercase tracking-wider text-slate-700">
          {STATE_LABELS[state]}
        </h2>
        {/* 计数用 (n) 而不是圆形气泡：等宽下对齐稳定 */}
        <span className="ml-auto font-mono text-xs text-slate-500 tabular-nums">
          ({tasks.length})
        </span>
      </header>

      {tasks.length === 0 ? (
        // 空桶显示波浪号，vim/终端里表示「此处无内容」
        <p className="py-6 text-center font-mono text-xs text-slate-300">~</p>
      ) : (
        // gap-px：行与行之间只留一像素，密排
        <ul className="flex flex-col gap-px p-px">
          {/* 真实数据里同一 task_id 可能在桶内出现多次（同名任务被重投递），
              所以 key 不能只用 task_id，否则 React 会报重复 key。 */}
          {tasks.map((task, i) => (
            <li key={`${state}:${task.task_id}:${task.mtime}:${i}`}>
              <TaskCard task={task} state={state} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** 首页：五状态桶看板，60 秒轮询 GET /api/tasks */
export default function TaskList() {
  const fetcher = useCallback(fetchTasks, [])
  const { data, error, lastUpdated, loading, refresh } = usePolling<TaskBuckets>(
    fetcher,
    POLL_INTERVAL_MS,
  )

  const buckets = normalize(data)
  const total = BUCKET_ORDER.reduce((sum, s) => sum + buckets[s].length, 0)
  const isFirstLoad = loading && data === null && error === null

  // KeyBar 上标了 `r`，就得真的能按。输入框里打字时不劫持按键。
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'r' || e.metaKey || e.ctrlKey || e.altKey) return
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || el?.isContentEditable) return
      e.preventDefault()
      refresh()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [refresh])

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto max-w-7xl px-4 py-6">
        {/* 终端标题栏：反白的路径式标题，右侧挂刷新 */}
        <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border border-slate-800 bg-slate-800 px-2 py-1">
          <h1 className="font-mono text-xs font-bold uppercase tracking-widest text-white">
            factory/queue
          </h1>
          <span className="font-mono text-xs text-slate-300 tabular-nums">
            {total} tasks
          </span>
          <div className="ml-auto flex items-baseline gap-3">
            <span className="font-mono text-xs text-slate-400 tabular-nums">
              {lastUpdated ? formatClock(lastUpdated) : '--:--:--'}
            </span>
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              className="font-mono text-xs text-cyan-300 underline-offset-2 transition hover:text-cyan-200 hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? '[refreshing]' : '[r] refresh'}
            </button>
          </div>
        </header>

        {/* 桶计数一行密排，不占竖向空间 */}
        <div className="border-x border-b border-slate-300 bg-slate-50 px-2 py-1">
          <StatusBar
            items={BUCKET_ORDER.map((s) => ({
              label: STATE_LABELS[s],
              value: buckets[s].length,
              tone: STATE_DOT_CLASSES[s],
            }))}
          />
        </div>

        {error !== null && (
          // 终端里的告警行：ERR 前缀 + 左侧红条，不用圆角提示框
          <div
            role="alert"
            className="border-x border-b border-l-2 border-slate-300 border-l-red-600 bg-red-50 px-2 py-1 font-mono text-xs"
          >
            <span className="font-bold text-red-700">ERR</span>{' '}
            <span className="text-slate-700">刷新失败，显示的是上次数据。</span>{' '}
            <span className="text-slate-500">{error}</span>
          </div>
        )}

        {isFirstLoad ? (
          <p className="border-x border-b border-slate-300 py-10 text-center font-mono text-xs text-slate-400">
            loading…
          </p>
        ) : (
          // gap-2：列间距收紧，五桶一屏
          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3 xl:grid-cols-5">
            {BUCKET_ORDER.map((state) => (
              <BucketColumn key={state} state={state} tasks={buckets[state]} />
            ))}
          </div>
        )}

        {/* 只列真正绑了实现的键：r 有 onClick，其余是导航路径提示 */}
        <div className="mt-2 border border-slate-300">
          <KeyBar
            hints={[
              { key: 'r', action: '刷新' },
              { key: '/submit', action: '投递任务' },
              { key: '/stats', action: '统计' },
              { key: 'click', action: '看任务详情' },
            ]}
          />
        </div>
      </div>
    </div>
  )
}
