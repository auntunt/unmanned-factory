import { Badge, Empty, Tag } from 'antd'
import { C, NODE_COLORS } from '../theme/tokens'
import { NODES } from '../theme/nodes'
import FactoryTaskCard, { type TaskRow } from './FactoryTaskCard'

/**
 * 列表看板 —— 7 个环形节点 + 阻塞 + 搁置 = 9 列。
 * 和环形图共用同一套 bucket 逻辑，避免两处口径漂移。
 */
const COLUMNS = [
  ...NODES.map((n) => ({ key: n.key, name: n.name, source: n.source, desc: n.desc })),
  { key: 'blocked', name: '阻塞', source: 'real' as const, desc: '依赖未就绪或环境坏了，自动链路走不动。' },
  { key: 'parked', name: '搁置', source: 'real' as const, desc: '主动挂起，不计入闭环分母。' },
]

export interface Buckets {
  [nodeKey: string]: TaskRow[]
}

interface Props {
  buckets: Buckets
  onTaskClick?: (t: TaskRow) => void
}

export default function KanbanView({ buckets, onTaskClick }: Props) {
  return (
    <div style={{ display: 'flex', gap: 12, overflowX: 'auto', paddingBottom: 8 }}>
      {COLUMNS.map((col) => {
        const list = buckets[col.key] ?? []
        const color = NODE_COLORS[col.key]?.main ?? C.error
        return (
          <div
            key={col.key}
            style={{
              flex: '0 0 268px',
              background: C.bgHead,
              border: `1px solid ${C.borderLight}`,
              borderRadius: 10,
              display: 'flex',
              flexDirection: 'column',
              maxHeight: 620,
            }}
          >
            <div
              style={{
                padding: '10px 12px',
                borderBottom: `1px solid ${C.borderLight}`,
                borderTop: `3px solid ${color}`,
                borderRadius: '10px 10px 0 0',
                background: C.bgCard,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
              >
                <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{col.name}</span>
                <Badge
                  count={list.length}
                  showZero
                  style={{ backgroundColor: list.length ? color : C.border }}
                />
              </div>
              {col.source === 'derived' && (
                <Tag
                  bordered={false}
                  style={{
                    marginTop: 6,
                    fontSize: 10,
                    lineHeight: '16px',
                    padding: '0 4px',
                    background: C.borderLight,
                    color: C.textSub,
                  }}
                >
                  派生态 · 停留极短
                </Tag>
              )}
            </div>
            <div style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
              {list.length === 0 ? (
                <Empty
                  imageStyle={{ height: 40 }}
                  style={{ margin: '32px 0' }}
                  description={
                    <span style={{ fontSize: 11, color: C.textDisabled }}>
                      {col.source === 'derived' ? '瞬时态，实测常为空' : '暂无任务'}
                    </span>
                  }
                />
              ) : (
                list.map((t) => (
                  <FactoryTaskCard key={t.task_id} task={t} onClick={onTaskClick} />
                ))
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
