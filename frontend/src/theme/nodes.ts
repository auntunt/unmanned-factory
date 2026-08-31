/**
 * 闭环节点定义 —— 工厂版
 *
 * 沿用复刻件最重要的一条设计（要点 #1）：
 * 队列里只有 6 个真实目录（inbox/running/done/needs-human/blocked/parked），
 * 环上却画 7 个节点，其中 judging / reworking 是**派生瞬时态** ——
 * 由 attempt 的 supervisors + resolution 推导，任务在这两环停留极短。
 *
 * 对照件的教训写在这里，别删：真实系统后端 4 状态、前端画 8 节点，
 * 4 个恒为 0 且不加任何标注，运维会读成「那个环节没跑」，实际是停留 < 1s。
 * 所以每个节点必须带 source，前端必须把 derived 显式画出来。
 */

export type NodeSource = 'real' | 'derived'

export interface ClosureNode {
  key: string
  name: string
  role: string
  source: NodeSource
  derivedFrom?: string
  bypass?: boolean
  terminal?: boolean
  desc: string
}

export const NODES: ClosureNode[] = [
  {
    key: 'inbox',
    name: '待派发',
    role: '投递人 / 调度器',
    source: 'real',
    desc: '任务已投递，等调度器领走。停在这里久了是调度没跑，不是任务有问题。',
  },
  {
    key: 'running',
    name: '执行中',
    role: 'worker（claude CLI）',
    source: 'real',
    desc: 'worker 正在沙箱里改代码。',
  },
  {
    key: 'judging',
    name: '监工裁决',
    role: '五个监工（回归/规格/架构/风险/范围）',
    source: 'derived',
    derivedFrom: '末轮已有 supervisor 裁决且 resolution 仍为 pending',
    desc: '监工正在对这一轮产出投票。停留极短，看到 0 是正常的。',
  },
  {
    key: 'reworking',
    name: '返工中',
    role: 'worker（下一轮）',
    source: 'derived',
    derivedFrom: '末轮 resolution = reworked',
    desc: '有监工投了 FAIL，任务回炉重跑。注意：这里存在轮次膨胀风险，需设上限。',
  },
  {
    key: 'done',
    name: '已定案',
    role: '归档',
    source: 'real',
    desc: '任务已出队归档。定案结果分 merged / escalated 两类，看颜色区分。',
  },
  {
    key: 'needs_human',
    name: '待人介入',
    role: '人（你）',
    source: 'real',
    desc: '自动链路走不下去，等人判断。这一环是当前最大的卡点。',
  },
  {
    key: 'merged',
    name: '已合并',
    role: '闭环完成',
    source: 'real',
    terminal: true,
    desc: '末轮 resolution = merged，真正闭环。',
  },
]

/** 队列目录名 → 展示节点。blocked / parked 不在环上，画在中心区。 */
export const STATUS_TO_NODE: Record<string, string> = {
  inbox: 'inbox',
  running: 'running',
  done: 'done',
  needs_human: 'needs_human',
  blocked: 'blocked',
  parked: 'parked',
}

/**
 * 主流程：相邻节点顺时针串联。
 * 不连成闭合环 —— 已合并 → 待派发 是不存在的流转，画上去会被读成有回路。
 */
export const MAIN_LINKS: [number, number][] = NODES.slice(0, -1).map((_, i) => [i, i + 1])

/** 旁路 / 异步：实线走弧，虚线走弦 */
export const BYPASS_LINKS: [number, number][] = [
  [0, 5], // 待派发 → 待人介入（预分级判成 C/D 类，根本不派发）
  [1, 4], // 执行中 → 已定案（一轮过，没经过返工）
  [2, 3], // 监工裁决 → 返工（有 FAIL）
  [3, 2], // 返工 → 监工裁决（下一轮再判，实测最深 9 轮）
  [2, 6], // 监工裁决 → 已合并（全 PASS 直接定案）
  [4, 5], // 已定案 → 待人介入（定案成 escalated）
  [4, 6], // 已定案 → 已合并（定案成 merged）
]
