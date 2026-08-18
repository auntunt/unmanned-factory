import { useCallback } from 'react'
import axios from 'axios'
import type { QueueState, TaskBuckets, TaskSummary } from '../types'
import { usePolling } from '../hooks/usePolling'
import TaskCard from '../components/TaskCard'
import { STATE_DOT_CLASSES, STATE_LABELS } from '../components/StateBadge'

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
    // min-h：任务少的时候五个桶不至于全塌在页面顶部，留出看板的形状。
    <section className="flex min-w-0 flex-col rounded-xl bg-slate-50 p-3 md:min-h-[22rem]">
      <header className="mb-3 flex items-center gap-2 border-b border-slate-200 pb-2">
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${STATE_DOT_CLASSES[state]}`}
          aria-hidden="true"
        />
        <h2 className="text-sm font-semibold text-slate-700">
          {STATE_LABELS[state]}
        </h2>
        <span className="ml-auto rounded-full bg-white px-2 py-0.5 text-xs font-medium text-slate-500 tabular-nums">
          {tasks.length}
        </span>
      </header>

      {tasks.length === 0 ? (
        <p className="py-6 text-center text-xs text-slate-400">暂无任务</p>
      ) : (
        <ul className="flex flex-col gap-2">
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

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto max-w-7xl px-4 py-6">
        <header className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <h1 className="text-xl font-semibold text-slate-900">任务看板</h1>
          <span className="text-sm text-slate-500 tabular-nums">
            共 {total} 个任务
          </span>

          <div className="ml-auto flex items-center gap-3">
            <span className="text-xs text-slate-500 tabular-nums">
              {lastUpdated
                ? `上次更新：${formatClock(lastUpdated)}`
                : '尚未更新'}
            </span>
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
        </header>

        {error !== null && (
          <div
            role="alert"
            className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
          >
            <span className="font-medium">刷新失败，显示的是上次数据。</span>{' '}
            <span className="text-amber-700">{error}</span>
          </div>
        )}

        {isFirstLoad ? (
          <p className="mt-10 text-center text-sm text-slate-400">加载中…</p>
        ) : (
          <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-3 xl:grid-cols-5">
            {BUCKET_ORDER.map((state) => (
              <BucketColumn key={state} state={state} tasks={buckets[state]} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
