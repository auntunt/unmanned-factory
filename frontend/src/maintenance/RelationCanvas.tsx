// 工作画布：代码库 → 需求 → 任务 → 产物/目标/阻塞 的关系投影。
// 纯 SVG/HTML 实现，不引入新依赖；节点位置由稳定布局计算，不持有业务状态。
import { useEffect, useMemo, useRef, useState } from 'react'

import { EmptyState } from '../workbench/ui'
import type { Graph, GraphNode, NodeType } from './types'

const COLUMN_ORDER: Record<NodeType, number> = {
  repo: 0,
  requirement: 1,
  task: 2,
  artifact: 3,
  target: 3,
  blocker: 3,
}

const NODE_WIDTH = 188
const NODE_HEIGHT = 74
const COLUMN_GAP = 260
const ROW_GAP = 112
const PADDING = 40

const MIN_SCALE = 0.3
const MAX_SCALE = 2.5

interface LaidOutNode extends GraphNode {
  x: number
  y: number
}

/** Tree layout by subtree size.
 *
 *  Each node is placed under its first parent, and every node reserves as many
 *  rows as its subtree has leaves. A task with two patches, a target and a
 *  blocker therefore takes four rows, and the next task starts below all of
 *  them -- the group never borrows another task's row. Columns stay by type. */
export function layout(nodes: GraphNode[], edges: Graph['edges'] = []): { nodes: LaidOutNode[]; width: number; height: number } {
  const byId = new Map(nodes.map(n => [n.id, n]))
  const children = new Map<string, string[]>()
  const hasParent = new Set<string>()
  for (const edge of edges) {
    if (!byId.has(edge.from) || !byId.has(edge.to) || hasParent.has(edge.to)) continue
    hasParent.add(edge.to)
    children.set(edge.from, [...(children.get(edge.from) ?? []), edge.to])
  }
  const rowsMemo = new Map<string, number>()
  const rows = (id: string, seen: Set<string> = new Set()): number => {
    if (rowsMemo.has(id)) return rowsMemo.get(id)!
    if (seen.has(id)) return 1
    seen.add(id)
    const total = (children.get(id) ?? []).reduce((sum, child) => sum + rows(child, seen), 0)
    const value = Math.max(1, total)
    rowsMemo.set(id, value)
    return value
  }
  const laidOut: LaidOutNode[] = []
  const placed = new Set<string>()
  let maxCol = 0
  const place = (id: string, row: number) => {
    if (placed.has(id)) return
    placed.add(id)
    const node = byId.get(id)!
    const col = COLUMN_ORDER[node.type] ?? 3
    maxCol = Math.max(maxCol, col)
    laidOut.push({ ...node, x: PADDING + col * COLUMN_GAP, y: PADDING + row * ROW_GAP })
    let childRow = row
    for (const child of children.get(id) ?? []) {
      place(child, childRow)
      childRow += rows(child)
    }
  }
  let nextRow = 0
  // Roots in their original order (repositories first, as the backend sends them).
  for (const node of nodes) {
    if (hasParent.has(node.id) || placed.has(node.id)) continue
    place(node.id, nextRow)
    nextRow += rows(node.id)
  }
  const width = PADDING * 2 + (maxCol + 1) * COLUMN_GAP
  const height = PADDING * 2 + Math.max(1, nextRow) * ROW_GAP
  return { nodes: laidOut, width, height }
}

function edgePath(from: LaidOutNode, to: LaidOutNode): string {
  const x1 = from.x + NODE_WIDTH
  const y1 = from.y + NODE_HEIGHT / 2
  const x2 = to.x
  const y2 = to.y + NODE_HEIGHT / 2
  const midX = (x1 + x2) / 2
  return `M ${x1} ${y1} H ${midX} V ${y2} H ${x2}`
}

const TYPE_LABEL: Record<NodeType, string> = {
  repo: '代码库', requirement: '需求', task: '任务', artifact: '产物', target: '目标', blocker: '阻塞',
}

export default function RelationCanvas({ graph, onOpenTask }: {
  graph: Graph
  onOpenTask: (taskId: string) => void
}) {
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const [scale, setScale] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const dragRef = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null)

  const { nodes: laidOutNodes, width: worldWidth, height: worldHeight } = useMemo(
    () => layout(graph.nodes, graph.edges),
    [graph.nodes, graph.edges],
  )
  const nodeById = useMemo(() => new Map(laidOutNodes.map(n => [n.id, n])), [laidOutNodes])
  const selected = selectedId ? nodeById.get(selectedId) ?? null : null

  function clampScale(next: number) {
    return Math.min(MAX_SCALE, Math.max(MIN_SCALE, next))
  }

  function zoomBy(factor: number, anchorX?: number, anchorY?: number) {
    setScale(prevScale => {
      const nextScale = clampScale(prevScale * factor)
      const ax = anchorX ?? (canvasRef.current?.clientWidth ?? 0) / 2
      const ay = anchorY ?? (canvasRef.current?.clientHeight ?? 0) / 2
      setPan(prevPan => ({
        x: ax - (ax - prevPan.x) * (nextScale / prevScale),
        y: ay - (ay - prevPan.y) * (nextScale / prevScale),
      }))
      return nextScale
    })
  }

  function fitView() {
    const el = canvasRef.current
    const viewportWidth = el?.clientWidth || 900
    const viewportHeight = el?.clientHeight || 560
    const nextScale = clampScale(Math.min(
      1,
      (viewportWidth - 24) / worldWidth,
      (viewportHeight - 24) / worldHeight,
    ))
    setScale(nextScale)
    setPan({ x: 12, y: 12 })
  }

  const fittedRef = useRef(false)
  useEffect(() => {
    if (fittedRef.current || laidOutNodes.length === 0) return
    fittedRef.current = true
    fitView()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [laidOutNodes.length])

  function handleWheel(e: React.WheelEvent<HTMLDivElement>) {
    e.preventDefault()
    const rect = e.currentTarget.getBoundingClientRect()
    zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - rect.left, e.clientY - rect.top)
  }

  function handlePointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest('button')) return
    dragRef.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  function handlePointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current
    if (!drag) return
    setPan({ x: drag.panX + (e.clientX - drag.x), y: drag.panY + (e.clientY - drag.y) })
  }

  function handlePointerUp() {
    dragRef.current = null
  }

  if (graph.nodes.length === 0) {
    return <EmptyState title="暂无可投影的对象" description="代码库与任务接入后会生成工作关系图。" />
  }

  return (
    <div className="mn-canvas-wrap">
      <div>
        <div className="mn-canvasbar">
          <small>代码库 → 需求 → 任务 → 产物。拖动背景平移，滚轮缩放，点击节点查看详情。</small>
          <button type="button" className="wb-button" aria-label="缩小画布" onClick={() => zoomBy(1 / 1.2)}>−</button>
          <span className="mn-zoom-readout">{Math.round(scale * 100)}%</span>
          <button type="button" className="wb-button" aria-label="放大画布" onClick={() => zoomBy(1.2)}>＋</button>
          <button type="button" className="wb-button" onClick={fitView}>适应视图</button>
        </div>
        {graph.truncated && <p className="mn-hint">对象较多，已按项目聚合显示部分。</p>}
        <div
          className="mn-canvas"
          ref={canvasRef}
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
        >
          <div
            className="mn-world"
            style={{ width: worldWidth, height: worldHeight, transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})` }}
          >
            <svg width={worldWidth} height={worldHeight}>
              <g fill="none" stroke="var(--wb-border-strong)" strokeWidth={2}>
                {graph.edges.map((edge, i) => {
                  const from = nodeById.get(edge.from)
                  const to = nodeById.get(edge.to)
                  if (!from || !to) return null
                  return <path key={`${edge.from}-${edge.to}-${i}`} d={edgePath(from, to)} />
                })}
              </g>
            </svg>
            {laidOutNodes.map(node => (
              <button
                type="button"
                key={node.id}
                className={`mn-node mn-node-${node.type}${selectedId === node.id ? ' is-selected' : ''}`}
                style={{ left: node.x, top: node.y, width: NODE_WIDTH }}
                onClick={() => setSelectedId(node.id)}
              >
                <span className="mn-node-label" title={node.label}>{node.label}</span>
                <small title={node.sublabel}>{node.sublabel}</small>
                {node.type === 'target' && <small>目标，尚未生成，不计入交付</small>}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="mn-canvas-side">
        {selected
          ? (
            <div className="mn-detail wb-card">
              <h2>{selected.label}</h2>
              <dl>
                <dt>类型</dt>
                <dd>{TYPE_LABEL[selected.type]}</dd>
                <dt>说明</dt>
                <dd>{selected.sublabel || '—'}</dd>
                <dt>状态</dt>
                <dd>{selected.status || '—'}</dd>
                <dt>对象 ID</dt>
                <dd className="mn-detail-id">{selected.id}</dd>
              </dl>
              {selected.task_id && (
                <button type="button" className="wb-button wb-button-primary" onClick={() => onOpenTask(selected.task_id as string)}>
                  进入任务现场
                </button>
              )}
            </div>
          )
          : <EmptyState title="点击节点查看详情" description="选中一个节点后，这里会显示它的类型、状态与对象 ID。" />}
      </div>
    </div>
  )
}
