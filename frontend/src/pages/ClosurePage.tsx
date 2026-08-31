import { useMemo, useState } from 'react'
import { Alert, Card, Radio, Space, Spin, Tag } from 'antd'
import ClosureRingView, { type RingCenter } from '../components/ClosureRingView'
import KanbanView from '../components/KanbanView'
import NodeDrawer from '../components/NodeDrawer'
import KpiHeader, { type KpiCounter, type KpiRing } from '../components/KpiHeader'
import type { ClosureNode } from '../theme/nodes'
import type { TaskRow } from '../components/FactoryTaskCard'
import { bucketTasks, countsFromBuckets, type ApiTasks } from '../lib/bucket'
import { C } from '../theme/tokens'
import { safeRate, fmtUsd, fmtRate } from '../theme/geometry'

interface Analytics {
  core: any
  cost: any
  stuck: any[]
  degraded?: string[]
  window_days: number
}

interface Props {
  tasks: ApiTasks | null
  analytics: Analytics | null
  loading: boolean
  onTaskClick?: (t: TaskRow) => void
}

export default function ClosurePage({ tasks, analytics, loading, onTaskClick }: Props) {
  const [view, setView] = useState<'ring' | 'kanban'>('ring')
  const [openNode, setOpenNode] = useState<ClosureNode | null>(null)

  const buckets = useMemo(() => bucketTasks(tasks ?? {}), [tasks])
  const counts = useMemo(() => countsFromBuckets(buckets), [buckets])

  if (loading && !tasks) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }

  const core = analytics?.core
  const cost = analytics?.cost
  const total = Object.values(buckets).reduce((s, v) => s + v.length, 0)
  const merged = buckets.merged?.length ?? 0
  const inflight = (buckets.inbox?.length ?? 0) + (buckets.running?.length ?? 0)

  const center: RingCenter = {
    // 分母用「已定案 + 已合并」而不是全部任务：还在跑的任务谈闭环率没意义
    rate: safeRate(merged, merged + (buckets.done?.length ?? 0)),
    total,
    merged,
    inflight,
    escalated: buckets.done?.length ?? 0,
    blocked: buckets.blocked?.length ?? 0,
    parked: buckets.parked?.length ?? 0,
  }

  const rings: KpiRing[] = [
    {
      key: 'auto',
      title: '自闭环率',
      rate: core?.auto_resolved_rate?.value ?? center.rate,
      scope: `全窗口 ${analytics?.window_days ?? '—'} 天`,
      sub: `${merged}/${merged + (buckets.done?.length ?? 0)} 已定案任务`,
      color: C.success,
    },
    {
      key: 'recent',
      title: '自闭环率',
      rate: core?.recent_auto_rate?.value ?? null,
      scope: '近 5 个定案',
      sub: '看趋势，不看总量',
      color: '#36CFC9',
    },
    {
      key: 'human',
      title: '人工验收通过率',
      rate: core?.human_pass_rate?.value ?? null,
      scope: '仅已验收样本',
      // 实测 payload 里 metric 只有 value 一个键，没有 numerator/denominator，
      // 之前照复刻件写的 `覆盖 x/y` 会渲染成「覆盖 undefined/undefined」。
      // 另外 human_coverage.value 是 0.0（不是 null），必须按 0 判而不是判空。
      sub: !core?.human_coverage?.value
        ? '人工验收覆盖 0 —— 没有人验过'
        : `人工验收覆盖 ${fmtRate(core.human_coverage.value, 0)}`,
      color: C.primary,
      weak: !core?.human_coverage?.value,
    },
  ]

  const counters: KpiCounter[] = [
    {
      key: 'depth',
      label: '平均轮次/任务',
      value: core?.rework_depth?.value?.toFixed?.(2) ?? '—',
      color: '#FFA940',
      icon: 'thunder',
    },
    {
      key: 'stuck',
      label: '滞留任务',
      value: analytics?.stuck?.length ?? 0,
      color: C.error,
      icon: 'clock',
    },
    {
      key: 'human',
      label: '待人介入',
      value: buckets.needs_human?.length ?? 0,
      color: '#FFA940',
      icon: 'close',
    },
    {
      key: 'merged',
      label: '已合并',
      value: merged,
      color: '#73D13D',
      icon: 'check',
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {analytics?.degraded && analytics.degraded.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="部分数据源读不到，页面处于降级状态"
          description={
            <span style={{ fontSize: 12 }}>
              失败的源：{analytics.degraded.join('、')}。
              这些模块显示的不是「没有数据」，而是「没读到数据」—— 别按 0 解读。
            </span>
          }
        />
      )}

      <KpiHeader
        rings={rings}
        counters={counters}
        costTotal={cost?.total_usd ?? 0}
        costWasted={cost?.wasted_usd ?? null}
        insight={
          cost?.wasted_usd != null && cost?.total_usd
            ? `${fmtUsd(cost.wasted_usd)} 花在末轮未合并的任务上，占总花费 ${(
                (cost.wasted_usd / cost.total_usd) * 100
              ).toFixed(0)}% —— 钱主要烧在没闭环的任务上，不是烧在产出上。`
            : '成本数据不足，无法归因。'
        }
        caveat={
          !core?.human_coverage?.value
            ? '人工验收覆盖为 0：所有「通过率」类指标都没有人工基准，现在只反映机器自评'
            : undefined
        }
      />

      <Card
        size="small"
        title="闭环链路"
        extra={
          <Space>
            <Tag bordered={false} style={{ fontSize: 11, color: C.textSub }}>
              实线=主流程 · 虚线=旁路/回流
            </Tag>
            <Radio.Group
              size="small"
              value={view}
              onChange={(e) => setView(e.target.value)}
              optionType="button"
              buttonStyle="solid"
            >
              <Radio.Button value="ring">环形</Radio.Button>
              <Radio.Button value="kanban">看板</Radio.Button>
            </Radio.Group>
          </Space>
        }
      >
        {view === 'ring' ? (
          <ClosureRingView counts={counts} center={center} onNodeClick={setOpenNode} />
        ) : (
          <KanbanView buckets={buckets} onTaskClick={onTaskClick} />
        )}
      </Card>

      <NodeDrawer
        node={openNode}
        tasks={openNode ? buckets[openNode.key] ?? [] : []}
        onClose={() => setOpenNode(null)}
        onTaskClick={onTaskClick}
      />
    </div>
  )
}
