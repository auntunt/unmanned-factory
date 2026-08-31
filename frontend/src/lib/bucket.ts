import { NODES } from '../theme/nodes'
import type { TaskRow } from '../components/FactoryTaskCard'

/**
 * 把 /api/tasks 的五个队列桶归集成 9 个展示节点。
 *
 * 单一出处：环形图和列表看板都调这里。复刻件的 KanbanView 和 NodeDrawer
 * 各写了一份 bucket 逻辑（还带 `??=` 副作用写法），两处一旦漂移，同一个
 * 节点在环上和看板上会显示不同的数 —— 这种不一致最难查。
 */

export interface ApiTasks {
  inbox?: TaskRow[]
  running?: TaskRow[]
  done?: TaskRow[]
  needs_human?: TaskRow[]
  blocked?: TaskRow[]
  parked?: TaskRow[]
}

export interface Buckets {
  [nodeKey: string]: TaskRow[]
}

const ALL_KEYS = [...NODES.map((n) => n.key), 'blocked', 'parked']

/**
 * done 桶按定案结果二分：merged 的落到「已合并」终态节点，
 * 其余（escalated / human_override）留在「已定案」。
 *
 * 不把全部 done 都算进「已合并」：那会让自闭环率虚高 —— 我们线上
 * 6 个 done 里只有 1 个真 merged，其余是升级给人处理的。
 */
export function bucketTasks(tasks: ApiTasks): Buckets {
  const out: Buckets = {}
  ALL_KEYS.forEach((k) => {
    out[k] = []
  })

  out.inbox = tasks.inbox ?? []
  out.running = tasks.running ?? []
  out.needs_human = tasks.needs_human ?? []
  out.blocked = tasks.blocked ?? []
  out.parked = tasks.parked ?? []

  for (const t of tasks.done ?? []) {
    if (t.resolution === 'merged') out.merged.push(t)
    else out.done.push(t)
  }

  // 派生态：judging / reworking 从队列里推不出来（队列目录没有这个维度）。
  // running 里 resolution=reworked 的算返工中，其余算裁决中 —— 但这只有
  // 后端补了末轮字段才准，没有就留空并靠 UI 的「派生态」标注说明。
  for (const t of out.running) {
    if (t.resolution === 'reworked') out.reworking.push(t)
  }
  out.running = out.running.filter((t) => t.resolution !== 'reworked')

  return out
}

export function countsFromBuckets(b: Buckets): Record<string, number> {
  const out: Record<string, number> = {}
  Object.entries(b).forEach(([k, v]) => {
    out[k] = v.length
  })
  return out
}
