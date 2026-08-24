import type { TaskDetail } from '../types'

export type StageStatus = 'done' | 'active' | 'pending' | 'skipped'

export interface Stage {
  no: number
  name: string
  status: StageStatus
  /** 该阶段真实数据的一句话摘要；skipped 时是「为什么没有」。 */
  detail: string
}

/**
 * 从任务详情推导九步流程状态。
 *
 * 诚实性约定：第 2（功能拆分）/ 第 7（总体测试）/ 第 8（冒烟测试）
 * 当前系统确实没有对应数据源，恒定为 skipped，不做任何推断填充。
 */
export function deriveStages(task: TaskDetail): Stage[] {
  const attempts = task.attempts
  const running = task.state === 'running'
  const merged = attempts.find((a) => a.resolution === 'merged')
  const dispatched = attempts.filter((a) => a.resolution !== 'not_dispatched')
  const latest = dispatched.length > 0 ? dispatched[dispatched.length - 1] : undefined
  const regressionCount = attempts.reduce(
    (n, a) => n + a.verdicts.filter((v) => v.role === 'regression').length,
    0,
  )

  const stages: Stage[] = []

  // 1 需求确认
  const promptLen = task.prompt.trim().length
  stages.push({
    no: 1,
    name: '需求确认',
    status: promptLen > 0 ? 'done' : 'pending',
    detail:
      promptLen > 0
        ? `prompt ${promptLen} 字 · 判据类 ${task.oracle_class}`
        : '尚无 prompt',
  })

  // 2 功能拆分 —— 无 WBS 数据，恒 skipped
  stages.push({
    no: 2,
    name: '功能拆分',
    status: 'skipped',
    detail: '单任务，未拆分',
  })

  // 3 技术选型
  const harness = latest ?? attempts[0]
  stages.push({
    no: 3,
    name: '技术选型',
    status: harness ? 'done' : 'pending',
    detail: harness
      ? `${harness.harness} · ${harness.harness_version}`
      : '尚未选定执行器',
  })

  // 4 系统设计（验收判据即设计契约）
  stages.push({
    no: 4,
    name: '系统设计',
    status: task.checks.length > 0 ? 'done' : 'skipped',
    detail:
      task.checks.length > 0
        ? `${task.checks.length} 条验收判据`
        : '未定义验收判据',
  })

  // 5 子代理执行
  stages.push({
    no: 5,
    name: '子代理执行',
    status: running ? 'active' : attempts.length > 0 ? 'done' : 'pending',
    detail:
      attempts.length > 0
        ? `${attempts.length} 轮 · $${task.total_cost_usd.toFixed(2)}`
        : '尚未派工',
  })

  // 6 单元测试（regression 监工判词）
  stages.push({
    no: 6,
    name: '单元测试',
    status: regressionCount > 0 ? 'done' : 'skipped',
    detail:
      regressionCount > 0
        ? `回归监工判词 ${regressionCount} 条`
        : '无回归判词记录',
  })

  // 7 / 8 —— 系统里没有独立环节，恒 skipped
  stages.push({
    no: 7,
    name: '总体测试',
    status: 'skipped',
    detail: '合并到监工判收',
  })
  stages.push({
    no: 8,
    name: '冒烟测试',
    status: 'skipped',
    detail: '合并到监工判收',
  })

  // 9 部署
  stages.push({
    no: 9,
    name: '部署',
    status: merged ? 'done' : running ? 'pending' : 'pending',
    detail: merged
      ? `已合入 ${merged.commit ? merged.commit.slice(0, 8) : '(无 commit 号)'}`
      : '未合入',
  })

  return stages
}

const STATUS_STYLE: Record<StageStatus, string> = {
  done: 'border-emerald-300 bg-emerald-50 text-emerald-900',
  active: 'border-sky-400 bg-sky-50 text-sky-900 animate-pulse',
  pending: 'border-slate-200 bg-white text-slate-500',
  // 虚线 + 灰底：和「未开始」（实线白底）拉开距离，扫一眼就能分出
  // 「系统没这个环节」和「还没跑到」——这两件事含义完全不同。
  skipped: 'border-2 border-dashed border-slate-400 bg-slate-100 text-slate-500',
}

const BADGE_STYLE: Record<StageStatus, string> = {
  done: 'bg-emerald-500 text-white',
  active: 'bg-sky-500 text-white',
  pending: 'bg-slate-300 text-slate-700',
  skipped: 'bg-slate-300 text-slate-500',
}

const STATUS_LABEL: Record<StageStatus, string> = {
  done: '已完成',
  active: '进行中',
  pending: '待进行',
  skipped: '暂无此环节',
}

interface PipelineStagesProps {
  task: TaskDetail
}

/** 顶部九步横向流程条。窄屏自动换行，不做横向滚动。 */
export default function PipelineStages({ task }: PipelineStagesProps) {
  const stages = deriveStages(task)
  const skipped = stages.filter((s) => s.status === 'skipped')

  return (
    <section aria-label="九步流程" className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-700">九步流程</h2>
        <span className="text-xs text-slate-400">
          {skipped.length > 0
            ? `${skipped.length} 个环节当前系统无数据，已标为「暂无此环节」`
            : '全部环节均有数据'}
        </span>
      </div>

      {/* 图例。没有它，外人只能靠颜色猜四种状态各是什么意思。 */}
      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-slate-100 pb-3 text-xs text-slate-500">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-emerald-300 bg-emerald-100" />
          已完成
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-sky-400 bg-sky-100" />
          进行中
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-slate-300 bg-white" />
          未开始
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm border border-dashed border-slate-400 bg-slate-100" />
          暂无此环节（当前系统没有这个阶段的数据，不是跑失败了）
        </span>
      </div>

      <ol className="flex flex-wrap items-stretch gap-y-3">
        {stages.map((stage, idx) => (
          <li key={stage.no} className="flex items-stretch">
            <div
              className={
                'flex w-[10.5rem] flex-col gap-1 rounded-md border px-3 py-2 ' +
                STATUS_STYLE[stage.status]
              }
              title={`${stage.name}：${STATUS_LABEL[stage.status]} · ${stage.detail}`}
            >
              <div className="flex items-center gap-2">
                <span
                  className={
                    'flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ' +
                    BADGE_STYLE[stage.status]
                  }
                >
                  {stage.no}
                </span>
                <span className="truncate text-sm font-medium">{stage.name}</span>
              </div>
              <span className="text-[11px] leading-snug">
                {stage.status === 'skipped' ? (
                  <>
                    <span className="font-medium">暂无此环节</span>
                    <span className="text-slate-400"> · {stage.detail}</span>
                  </>
                ) : (
                  stage.detail
                )}
              </span>
            </div>
            {idx < stages.length - 1 && (
              <span
                aria-hidden="true"
                className="flex items-center px-1 text-slate-300"
              >
                →
              </span>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}
