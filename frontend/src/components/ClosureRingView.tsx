import { Tooltip } from 'antd'
import { C, NODE_COLORS, RING } from '../theme/tokens'
import { NODES, MAIN_LINKS, BYPASS_LINKS, type ClosureNode } from '../theme/nodes'
import { nodePositions, arcBetween, chordBetween } from '../theme/geometry'
import { fmtRate } from '../theme/geometry'

const pos = nodePositions(NODES.length)

export interface RingCenter {
  rate: number | null
  total: number
  merged: number
  inflight: number
  escalated: number
  blocked: number
  parked: number
}

interface Props {
  counts: Record<string, number | null>
  center: RingCenter
  onNodeClick?: (n: ClosureNode) => void
}

export default function ClosureRingView({ counts, center, onNodeClick }: Props) {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: '20px 0' }}>
      <div style={{ position: 'relative', width: RING.size, height: RING.size }}>
        <svg width={RING.size} height={RING.size} style={{ position: 'absolute', inset: 0 }}>
          <circle
            cx={RING.size / 2}
            cy={RING.size / 2}
            r={RING.radius}
            fill="none"
            stroke={C.borderLight}
            strokeWidth={1}
          />
          {/* 旁路先画，压在主流程下面 */}
          {BYPASS_LINKS.map(([a, b], i) => (
            <path
              key={`b${i}`}
              d={chordBetween(pos[a], pos[b])}
              fill="none"
              stroke={C.slate}
              strokeWidth={1.5}
              strokeDasharray="5 5"
              opacity={0.55}
            />
          ))}
          <defs>
            {NODES.map((n) => (
              <marker
                key={n.key}
                id={`arrow-${n.key}`}
                viewBox="0 0 10 10"
                refX={9}
                refY={5}
                markerWidth={4}
                markerHeight={4}
                orient="auto"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill={NODE_COLORS[n.key].main} />
              </marker>
            ))}
          </defs>
          {MAIN_LINKS.map(([a, b], i) => (
            <path
              key={`m${i}`}
              d={arcBetween(pos[a], pos[b])}
              fill="none"
              stroke={NODE_COLORS[NODES[a].key].light}
              strokeWidth={2.5}
              opacity={0.85}
              markerEnd={`url(#arrow-${NODES[a].key})`}
            />
          ))}
        </svg>

        {/* 中心统计 */}
        <div
          style={{
            position: 'absolute',
            left: (RING.size - RING.centerSize) / 2,
            top: (RING.size - RING.centerSize) / 2,
            width: RING.centerSize,
            height: RING.centerSize,
            borderRadius: '50%',
            background: '#fff',
            border: `1px solid ${C.borderLight}`,
            boxShadow: '0 2px 12px rgba(0,0,0,0.06)',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 2,
          }}
        >
          <div style={{ fontSize: 12, color: C.textSub }}>自闭环率</div>
          <div
            style={{
              fontSize: 40,
              fontWeight: 600,
              color: center.rate == null ? C.textDisabled : C.success,
              lineHeight: 1.1,
            }}
          >
            {fmtRate(center.rate, 1)}
          </div>
          <div style={{ fontSize: 12, color: C.textSub }}>
            共 {center.total} 单 · 已合并 {center.merged}
          </div>
          <div
            style={{
              marginTop: 6,
              fontSize: 11,
              color: C.textDisabled,
              textAlign: 'center',
              lineHeight: 1.7,
            }}
          >
            流转中 {center.inflight} · 升级 {center.escalated}
            <br />
            阻塞 {center.blocked} · 搁置 {center.parked}
          </div>
        </div>

        {/* 节点 */}
        {NODES.map((node, i) => {
          const p = pos[i]
          const col = NODE_COLORS[node.key]
          const raw = counts[node.key]
          const n = raw ?? 0
          // 派生态为 0 时压暗，并在 tooltip 里说明「不是没跑」
          const dim = node.source === 'derived' && n === 0
          return (
            <Tooltip
              key={node.key}
              title={
                <div style={{ fontSize: 12, lineHeight: 1.7 }}>
                  <div style={{ fontWeight: 600 }}>{node.name}</div>
                  <div>{node.desc}</div>
                  <div style={{ opacity: 0.75, marginTop: 4 }}>
                    {node.source === 'real'
                      ? '数据来源：队列目录（真实状态）'
                      : `数据来源：前端派生 ← ${node.derivedFrom}`}
                  </div>
                  {raw == null && (
                    <div style={{ color: C.warning, marginTop: 4 }}>
                      队列读不到，这个 0 不可信
                    </div>
                  )}
                </div>
              }
            >
              <button
                onClick={() => onNodeClick?.(node)}
                style={{
                  position: 'absolute',
                  left: p.x - RING.nodeSize / 2,
                  top: p.y - RING.nodeSize / 2,
                  width: RING.nodeSize,
                  height: RING.nodeSize,
                  borderRadius: '50%',
                  border: `2px solid ${dim ? C.border : col.main}`,
                  background: dim
                    ? '#fff'
                    : `linear-gradient(135deg, ${col.light} 0%, ${col.main} 100%)`,
                  color: dim ? C.textDisabled : '#fff',
                  cursor: 'pointer',
                  padding: 0,
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: dim ? 'none' : `0 3px 10px ${col.main}44`,
                  transition: 'transform .15s',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.transform = 'scale(1.08)'
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.transform = 'scale(1)'
                }}
              >
                <span style={{ fontSize: 18, fontWeight: 600, lineHeight: 1 }}>
                  {raw == null ? '?' : n}
                </span>
                <span style={{ fontSize: 10, marginTop: 2 }}>{node.name}</span>
              </button>
            </Tooltip>
          )
        })}
      </div>
    </div>
  )
}
