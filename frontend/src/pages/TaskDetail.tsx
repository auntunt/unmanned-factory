import { useCallback } from 'react'
import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import axios from 'axios'
import type { QueueState, TaskDetail as TaskDetailData } from '../types'
import { usePolling } from '../hooks/usePolling'
import PipelineStages from '../components/PipelineStages'
import AttemptTimeline from '../components/AttemptTimeline'
import { StatusBar } from '../components/ConsoleChrome'
import { fmtDuration } from '../lib/attemptFormat'
import { resolutionText } from '../lib/humanize'

const POLL_INTERVAL_MS = 60_000

const STATE_LABEL: Record<QueueState, string> = {
  inbox: '待处理',
  running: '运行中',
  done: '已完成',
  needs_human: '待人工',
  blocked: '已阻塞',
}

const STATE_STYLE: Record<QueueState, string> = {
  inbox: 'bg-slate-100 text-slate-700 border-slate-300',
  running: 'bg-sky-100 text-sky-800 border-sky-300',
  done: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  needs_human: 'bg-amber-100 text-amber-800 border-amber-300',
  blocked: 'bg-rose-100 text-rose-800 border-rose-300',
}

const CLOCK_FMT = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

async function fetchTask(taskId: string): Promise<TaskDetailData> {
  const res = await axios.get<TaskDetailData>(
    `/api/task/${encodeURIComponent(taskId)}`,
  )
  return res.data
}

/** 把 axios 错误翻成人话，404 单独识别出来给友好错误页用。 */
function describeError(raw: string): { notFound: boolean; message: string } {
  const notFound = /\b404\b|not found/i.test(raw)
  return { notFound, message: raw }
}

/**
 * 既能当路由页用（从 URL 读 taskId），也能被外壳以 props 驱动当全屏详情用。
 * props 优先：新外壳是单页页签结构，详情由状态而非 URL 驱动。
 */
export default function TaskDetail({
  taskId: taskIdProp,
  onBack,
}: {
  taskId?: string
  onBack?: () => void
} = {}) {
  const params = useParams<{ taskId: string }>()
  const taskId = taskIdProp ?? params.taskId
  void onBack

  const fetcher = useCallback(() => {
    if (!taskId) return Promise.reject(new Error('URL 里没有 task_id'))
    return fetchTask(taskId)
  }, [taskId])

  const { data, error, lastUpdated, loading, refresh } = usePolling<TaskDetailData>(
    fetcher,
    POLL_INTERVAL_MS,
  )

  if (loading && !data) {
    return (
      <Shell taskId={taskId}>
        <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
          正在加载任务详情…
        </div>
      </Shell>
    )
  }

  // 从未拉到过数据 + 有错误 → 友好错误页（不白屏）
  if (!data && error) {
    const { notFound, message } = describeError(error)
    return (
      <Shell taskId={taskId}>
        <div className="rounded-lg border border-rose-200 bg-rose-50 p-8">
          <h2 className="text-base font-semibold text-rose-900">
            {notFound ? '找不到这个任务' : '加载任务详情失败'}
          </h2>
          <p className="mt-2 text-sm text-rose-800">
            {notFound ? (
              <>
                任务 <code className="font-mono">{taskId}</code>{' '}
                不在审计库里。检查一下 task_id 拼写，或者它可能还没进过队列。
              </>
            ) : (
              <>后端没能返回数据：{message}</>
            )}
          </p>
          <div className="mt-4 flex gap-3 text-sm">
            <button
              type="button"
              onClick={refresh}
              className="rounded border border-rose-300 bg-white px-3 py-1.5 text-rose-800 hover:bg-rose-100"
            >
              重试
            </button>
            <Link
              to="/"
              className="rounded border border-slate-300 bg-white px-3 py-1.5 text-slate-700 hover:bg-slate-100"
            >
              返回任务列表
            </Link>
          </div>
        </div>
      </Shell>
    )
  }

  if (!data) {
    return (
      <Shell taskId={taskId}>
        <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
          没有可显示的数据。
        </div>
      </Shell>
    )
  }

  const running = data.state === 'running'
  // 真实派工过的轮次。未派工轮 cost 为 0，混在总数里会虚报工作量。
  const execRounds = data.attempts.filter((a) => (a.cost_usd ?? 0) > 0).length

  return (
    <Shell taskId={data.task_id}>
      <div className="space-y-3">
        <header className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="font-mono text-lg font-semibold text-slate-900">
              {data.task_id}
            </h1>
            <span
              className={
                'inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium ' +
                STATE_STYLE[data.state]
              }
            >
              {running && (
                <span className="relative flex h-2 w-2" aria-hidden="true">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sky-500 opacity-75" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-sky-600" />
                </span>
              )}
              {STATE_LABEL[data.state] ?? data.state}
            </span>
            <span className="ml-auto flex items-center gap-3 text-xs text-slate-400">
              {error && <span className="text-amber-600">刷新失败，显示的是上次数据</span>}
              <span>
                上次更新：
                {lastUpdated ? CLOCK_FMT.format(lastUpdated) : '—'}
              </span>
              <button
                type="button"
                onClick={refresh}
                className="rounded border border-slate-300 px-2 py-0.5 text-slate-600 hover:bg-slate-100"
              >
                手动刷新
              </button>
            </span>
          </div>

          <div className="mt-3 border-t border-slate-200 pt-2">
            <StatusBar
              items={[
                { label: 'cost', value: `$${data.total_cost_usd.toFixed(4)}` },
                { label: 'wall', value: fmtDuration(data.total_wall_clock_s) },
                {
                  label: 'attempts',
                  value: `${data.attempts.length}${
                    execRounds === data.attempts.length
                      ? ''
                      : ` (${execRounds} 真跑)`
                  }`,
                },
                {
                  label: 'oracle',
                  value: data.oracle_class,
                  tone: 'text-slate-600',
                },
              ]}
            />
            {data.class_reason && (
              <p className="mt-1 font-mono text-[10px] text-slate-400">
                oracle 依据：{data.class_reason}
              </p>
            )}
          </div>
        </header>

        <PipelineStages task={data} />

        <Conclusion task={data} />

        <section className="grid gap-4 lg:grid-cols-3">
          <div className="rounded-lg border border-slate-200 bg-white p-4 lg:col-span-2">
            <h2 className="mb-2 text-sm font-semibold text-slate-700">需求原文</h2>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3 text-xs leading-relaxed text-slate-700">
              {data.prompt}
            </pre>
          </div>

          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <h2 className="mb-2 text-sm font-semibold text-slate-700">
              验收判据（{data.checks.length}）
            </h2>
            {data.checks.length === 0 ? (
              <p className="text-xs text-slate-400">未定义验收判据</p>
            ) : (
              <ul className="space-y-2">
                {data.checks.map((check) => (
                  <li key={check.name} className="rounded bg-slate-50 p-2">
                    <div className="text-xs font-medium text-slate-700">
                      {check.name}
                    </div>
                    <div className="mt-0.5 max-h-20 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-slate-500">
                      {check.command}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        <AttemptTimeline
          attempts={data.attempts}
          taskId={data.task_id}
          // 介入成功后立刻拉一次，不等下一个轮询周期 —— 人点完「定案」
          // 要马上看到那一行变成 merged，否则会怀疑没生效再点一次。
          onIntervened={refresh}
        />
      </div>
    </Shell>
  )
}

function Shell({
  taskId,
  children,
}: {
  taskId?: string
  children: ReactNode
}) {
  return (
    <div className="min-h-screen bg-slate-50 px-4 py-6">
      <div className="mx-auto max-w-6xl">
        <nav className="mb-4 text-xs text-slate-400">
          <Link to="/" className="hover:text-slate-600 hover:underline">
            任务列表
          </Link>
          <span className="mx-1.5">/</span>
          <span className="font-mono text-slate-600">{taskId ?? '未知任务'}</span>
        </nav>
        {children}
      </div>
    </div>
  )
}

/**
 * 结论区块。放在流程条正下方，让人不用滚到时间线底部就知道结果。
 *
 * 只讲三件事：成没成、花了多少、试了几轮。技术细节留给下面的时间线。
 */
function Conclusion({ task }: { task: TaskDetailData }) {
  const last = task.attempts[task.attempts.length - 1]
  const merged = last?.resolution === 'merged'
  const attempts = task.attempts.length
  // 真跑过的轮次：cost 为 0 的是 harness 缺失空跑，算进去会让「平均每轮」失真。
  const realRuns = task.attempts.filter((a) => (a.cost_usd ?? 0) > 0).length

  // 左侧色条 + 稍深底色：这块是给外人看的第一落点，权重要压过下面的
  // 需求原文/时间线，否则演示时对方眼睛会先掉进代码块里。
  const tone = merged
    ? 'border-emerald-300 bg-emerald-50/80 border-l-4 border-l-emerald-500'
    : task.state === 'needs_human'
      ? 'border-amber-300 bg-amber-50/80 border-l-4 border-l-amber-500'
      : 'border-slate-200 bg-white border-l-4 border-l-slate-400'

  return (
    <section className={`rounded-lg border p-4 ${tone}`}>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-base leading-none">{merged ? '✅' : task.state === 'needs_human' ? '⚠️' : '•'}</span>
        <h2 className="text-base font-semibold text-slate-900">结论</h2>
      </div>
      <p className="text-[0.95rem] leading-relaxed text-slate-800">
        {merged ? (
          <>
            改动已合入代码库
            {last?.commit ? (
              <>
                （commit <code className="font-mono text-xs">{last.commit.slice(0, 8)}</code>）
              </>
            ) : null}
            ，共试了 <strong>{attempts}</strong> 轮
            {realRuns !== attempts ? `（其中 ${realRuns} 轮真实执行）` : null}，
            累计花费 <strong>${task.total_cost_usd.toFixed(2)}</strong>。
          </>
        ) : task.state === 'needs_human' ? (
          <>
            自动流程没能收尾，已经交回给你。试了 <strong>{attempts}</strong> 轮
            {realRuns !== attempts ? `（其中 ${realRuns} 轮真实执行）` : null}，
            花费 <strong>${task.total_cost_usd.toFixed(2)}</strong>。
            {last ? <> 最后一轮：{resolutionText(last.resolution).what}</> : null}
          </>
        ) : task.state === 'running' ? (
          <>
            正在跑第 <strong>{attempts}</strong> 轮，目前已花费{' '}
            <strong>${task.total_cost_usd.toFixed(2)}</strong>。
          </>
        ) : (
          <>
            当前状态：{STATE_LABEL[task.state] ?? task.state}。
            {attempts > 0 ? (
              <>
                {' '}
                已试 <strong>{attempts}</strong> 轮，花费{' '}
                <strong>${task.total_cost_usd.toFixed(2)}</strong>。
              </>
            ) : (
              ' 尚未派发执行。'
            )}
          </>
        )}
      </p>
    </section>
  )
}
