import { Alert, Drawer, Empty, Tag } from 'antd'
import { C, NODE_COLORS } from '../theme/tokens'
import type { ClosureNode } from '../theme/nodes'
import FactoryTaskCard, { type TaskRow } from './FactoryTaskCard'

/**
 * 第二层下钻：点环形节点 → 该节点下的任务列表。
 *
 * 列表由 bucketTasks() 统一算好后传进来，抽屉自己不再做一遍归集 ——
 * 复刻件在这里重算了一遍，是环上数与抽屉里数不一致的隐患来源。
 */

interface Props {
  node: ClosureNode | null
  tasks: TaskRow[]
  onClose: () => void
  onTaskClick?: (t: TaskRow) => void
}

export default function NodeDrawer({ node, tasks, onClose, onTaskClick }: Props) {
  if (!node) return null
  const col = NODE_COLORS[node.key] ?? NODE_COLORS.inbox

  return (
    <Drawer
      open
      onClose={onClose}
      width={560}
      styles={{ body: { background: C.bgPage } }}
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span
            style={{ width: 10, height: 10, borderRadius: '50%', background: col.main }}
          />
          <span>{node.name}</span>
          <Tag
            bordered={false}
            color={node.source === 'real' ? 'blue' : 'default'}
            style={{ fontSize: 11 }}
          >
            {node.source === 'real' ? '队列真实状态' : '前端派生态'}
          </Tag>
          <span style={{ fontSize: 12, color: C.textSub, fontWeight: 400 }}>
            {tasks.length} 个任务
          </span>
        </div>
      }
    >
      <div style={{ fontSize: 12, color: C.textSub, marginBottom: 12, lineHeight: 1.8 }}>
        {node.desc}
        <div style={{ marginTop: 4 }}>负责角色：{node.role}</div>
      </div>

      {node.source === 'derived' && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12, fontSize: 12 }}
          message="这是派生态，不是队列里的目录"
          description={`由末轮 attempt 字段推导（${node.derivedFrom}）。任务在这一环停留极短，看到 0 是正常的，不代表这一步没跑。要看真实执行记录，进任务详情看每轮的监工裁决。`}
        />
      )}

      {node.key === 'needs_human' && tasks.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12, fontSize: 12 }}
          message="这一环是当前最大的卡点"
          description="任务停在这里不烧钱，但也不前进。滞留超过 2 天的卡片已标红 —— 卡点不在机器，在人工介入环节。"
        />
      )}

      {tasks.length === 0 ? (
        <Empty description="该节点当前无任务" style={{ marginTop: 48 }} />
      ) : (
        tasks.map((t) => (
          <FactoryTaskCard key={t.task_id} task={t} onClick={onTaskClick} />
        ))
      )}
    </Drawer>
  )
}
