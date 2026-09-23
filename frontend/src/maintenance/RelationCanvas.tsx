// 工程关系画布：代码库 → 需求 → 任务 → 方案 → 检查 → 补丁/目标，阻塞挂在真正卡住的阶段上。
// 纯 SVG/HTML，不引入新依赖；节点位置由稳定布局计算，不持有业务状态。
// 所有事实来自服务端投影（已按调用者范围过滤）；这里只负责排布、选择和跳转。
import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent, ReactElement, WheelEvent } from 'react'
import { Link } from 'react-router-dom'

import { EmptyState } from '../workbench/ui'
import { useMaintenancePath } from './base-path'
import type { Graph, GraphEdge, GraphLink, GraphNode, NodeTone, NodeType } from './types'

const COLUMN_ORDER: Record<NodeType, number> = {
  repo: 0, requirement: 1, task: 2, plan: 3, check: 4, artifact: 5, target: 5, blocker: 5,
}
/** Stages hidden when the canvas is collapsed for density: the work items stay. */
const STAGE_TYPES: NodeType[] = ['plan', 'check', 'artifact', 'target']

const NODE_WIDTH = 196
const NODE_HEIGHT = 78
const COLUMN_GAP = 236
const ROW_GAP = 104
const PADDING = 32
const MIN_SCALE = 0.25
const MAX_SCALE = 2.5
/** A target is where a delivery would go; it never counts as one. */
const TARGET_NOTE = '目标，尚未生成，不计入交付'
/** Above this many nodes with no project chosen, stages start collapsed. */
export const DENSE_NODE_COUNT = 80

export const TYPE_LABEL: Record<NodeType, string> = {
  repo: '代码库', requirement: '需求', task: '任务', plan: '方案', check: '检查', artifact: '补丁', target: '目标', blocker: '待处理',
}
const TONE_LABEL: Record<NodeTone, string> = {
  ok: '正常', attention: '需要人处理', blocked: '受阻', pending: '进行中', none: '尚无记录',
}
const EDGE_LABEL: Record<GraphEdge['kind'], string> = {
  has: '包含', creates: '生成任务', plans: '制定方案', verified_by: '检查', produces: '产出', targets: '目标', blocked_by: '卡在',
}

interface LaidOutNode extends GraphNode {
  x: number
  y: number
}

/** Tree layout by subtree size: each node sits under its first parent and
 *  reserves as many rows as its subtree has leaves, so groups never share rows
 *  and edges only run right-and-down between columns. Columns stay by type. */
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
    const value = Math.max(1, (children.get(id) ?? []).reduce((sum, child) => sum + rows(child, seen), 0))
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
    const col = COLUMN_ORDER[node.type] ?? 5
    maxCol = Math.max(maxCol, col)
    laidOut.push({ ...node, x: PADDING + col * COLUMN_GAP, y: PADDING + row * ROW_GAP })
    let childRow = row
    for (const child of children.get(id) ?? []) {
      place(child, childRow)
      childRow += rows(child)
    }
  }
  let nextRow = 0
  for (const node of nodes) {
    if (hasParent.has(node.id) || placed.has(node.id)) continue
    place(node.id, nextRow)
    nextRow += rows(node.id)
  }
  return { nodes: laidOut, width: PADDING * 2 + (maxCol + 1) * COLUMN_GAP, height: PADDING * 2 + Math.max(1, nextRow) * ROW_GAP }
}

/** Drop stage nodes and re-attach anything that hung from them (blockers) to the nearest visible ancestor. */
export function collapseStages(graph: Graph): Graph {
  const hidden = new Set(graph.nodes.filter(n => STAGE_TYPES.includes(n.type)).map(n => n.id))
  const parentOf = new Map(graph.edges.map(e => [e.to, e.from]))
  const visibleAncestor = (id: string): string | undefined => {
    let current = parentOf.get(id)
    while (current && hidden.has(current)) current = parentOf.get(current)
    return current
  }
  const edges: GraphEdge[] = []
  for (const edge of graph.edges) {
    if (hidden.has(edge.to)) continue
    const from = hidden.has(edge.from) ? visibleAncestor(edge.to) : edge.from
    if (from) edges.push({ ...edge, from })
  }
  return { ...graph, nodes: graph.nodes.filter(n => !hidden.has(n.id)), edges }
}

function edgePath(from: LaidOutNode, to: LaidOutNode): string {
  const x1 = from.x + NODE_WIDTH
  const y1 = from.y + NODE_HEIGHT / 2
  const x2 = to.x
  const y2 = to.y + NODE_HEIGHT / 2
  // Turn just before the child's column: vertical runs sit in the gutter, never under text.
  const midX = x2 - 20
  return `M ${x1} ${y1} H ${midX} V ${y2} H ${x2}`
}

function NodeLinks({ links }: { links: GraphLink[] }) {
  const mp = useMaintenancePath()
  if (!links.length) return <p className="mn-hint">这个节点没有可进入的详情页。</p>
  return (
    <div className="mn-detail-links">
      {links.map(link => (
        <Link key={`${link.kind}-${link.id}-${link.label}`} className="wb-button wb-button-primary"
          to={link.kind === 'repo' ? mp(`/repos/${encodeURIComponent(link.id)}`) : mp(`/${encodeURIComponent(link.id)}`)}>
          {link.label}
        </Link>
      ))}
    </div>
  )
}

function NodeDetail({ node }: { node: GraphNode }) {
  return (
    <div className="mn-detail wb-card" aria-live="polite">
      <span className="wb-eyebrow">{TYPE_LABEL[node.type]} · {TONE_LABEL[node.tone ?? 'pending']}</span>
      <h2>{node.full_label ?? node.label}</h2>
      <p className="mn-hint">{node.sublabel}</p>
      <dl>
        {(node.facts ?? []).map(fact => <FactRow key={fact.label} label={fact.label} value={fact.value} />)}
        <dt>来源 ID</dt>
        <dd className="mn-detail-id">{node.id}</dd>
      </dl>
      <NodeLinks links={node.links ?? []} />
    </div>
  )
}

function FactRow({ label, value }: { label: string; value: string }) {
  return <><dt>{label}</dt><dd>{value}</dd></>
}

/** Hierarchical list of the same projection: every node reachable without a mouse. */
function GraphList({ graph, selectedId, onSelect }: { graph: Graph; selectedId: string | null; onSelect: (id: string) => void }) {
  const byId = new Map(graph.nodes.map(n => [n.id, n]))
  const children = new Map<string, GraphNode[]>()
  const hasParent = new Set<string>()
  for (const edge of graph.edges) {
    const child = byId.get(edge.to)
    if (!child || !byId.has(edge.from) || hasParent.has(edge.to)) continue
    hasParent.add(edge.to)
    children.set(edge.from, [...(children.get(edge.from) ?? []), child])
  }
  const render = (node: GraphNode): ReactElement => (
    <li key={node.id}>
      <button type="button" className={`mn-list-node mn-tone-${node.tone ?? 'pending'}${selectedId === node.id ? ' is-selected' : ''}`}
        aria-pressed={selectedId === node.id} onClick={() => onSelect(node.id)}>
        <span className="mn-node-type">{TYPE_LABEL[node.type]}</span>
        <strong>{node.label}</strong>
        <small>{node.type === 'target' ? TARGET_NOTE : node.sublabel}</small>
      </button>
      {(children.get(node.id) ?? []).length > 0 && <ul>{children.get(node.id)!.map(render)}</ul>}
    </li>
  )
  return <ul className="mn-graph-list" aria-label="关系列表">{graph.nodes.filter(n => !hasParent.has(n.id)).map(render)}</ul>
}

export interface CanvasState {
  mode: 'canvas' | 'list'
  project: string
  node: string | null
  /** null = automatic (collapsed only when dense and unfiltered). */
  collapsed: boolean | null
}

export default function RelationCanvas({ graph, state, onStateChange }: {
  graph: Graph
  state: CanvasState
  onStateChange: (next: Partial<CanvasState>) => void
}) {
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const [scale, setScale] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const dragRef = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null)

  const dense = graph.nodes.length > DENSE_NODE_COUNT && !state.project
  const collapsed = state.collapsed ?? dense
  const shown = useMemo(() => (collapsed ? collapseStages(graph) : graph), [graph, collapsed])
  const { nodes: laidOutNodes, width: worldWidth, height: worldHeight } = useMemo(
    () => layout(shown.nodes, shown.edges), [shown])
  const nodeById = useMemo(() => new Map(laidOutNodes.map(n => [n.id, n])), [laidOutNodes])
  const selected = state.node ? graph.nodes.find(n => n.id === state.node) ?? null : null

  const clampScale = (next: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, next))

  function zoomBy(factor: number, anchorX?: number, anchorY?: number) {
    setScale(prevScale => {
      const nextScale = clampScale(prevScale * factor)
      const ax = anchorX ?? (canvasRef.current?.clientWidth ?? 0) / 2
      const ay = anchorY ?? (canvasRef.current?.clientHeight ?? 0) / 2
      setPan(prevPan => ({ x: ax - (ax - prevPan.x) * (nextScale / prevScale), y: ay - (ay - prevPan.y) * (nextScale / prevScale) }))
      return nextScale
    })
  }

  function fitView() {
    const el = canvasRef.current
    const nextScale = clampScale(Math.min(1, ((el?.clientWidth || 900) - 24) / worldWidth, ((el?.clientHeight || 560) - 24) / worldHeight))
    setScale(nextScale)
    setPan({ x: 12, y: 12 })
  }

  function locateSelected() {
    const node = state.node ? nodeById.get(state.node) : undefined
    const el = canvasRef.current
    if (!node || !el) return
    const nextScale = Math.max(scale, 0.9)
    setScale(nextScale)
    setPan({ x: el.clientWidth / 2 - (node.x + NODE_WIDTH / 2) * nextScale, y: el.clientHeight / 2 - (node.y + NODE_HEIGHT / 2) * nextScale })
  }

  // Fit once per project/collapse/mode; coming back with a selected node centres on it instead.
  const fittedFor = useRef<string | null>(null)
  useEffect(() => {
    const key = `${state.project}|${collapsed}|${state.mode}`
    if (fittedFor.current === key || laidOutNodes.length === 0 || state.mode !== 'canvas') return
    fittedFor.current = key
    if (state.node && nodeById.has(state.node)) locateSelected()
    else fitView()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [laidOutNodes.length, state.project, collapsed, state.mode])

  function handleWheel(e: WheelEvent<HTMLDivElement>) {
    e.preventDefault()
    const rect = e.currentTarget.getBoundingClientRect()
    zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - rect.left, e.clientY - rect.top)
  }
  function handlePointerDown(e: PointerEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest('button, a')) return
    dragRef.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  function handlePointerMove(e: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current
    if (drag) setPan({ x: drag.panX + (e.clientX - drag.x), y: drag.panY + (e.clientY - drag.y) })
  }
  function handlePointerUp() { dragRef.current = null }
  function handleKey(e: KeyboardEvent<HTMLDivElement>) {
    if (e.target !== e.currentTarget) return
    const step = 60
    const moves: Record<string, [number, number]> = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }
    if (moves[e.key]) { e.preventDefault(); setPan(p => ({ x: p.x + moves[e.key][0], y: p.y + moves[e.key][1] })) }
    else if (e.key === '+' || e.key === '=') zoomBy(1.2)
    else if (e.key === '-') zoomBy(1 / 1.2)
    else if (e.key === '0') fitView()
  }

  const select = (id: string) => onStateChange({ node: id })

  return (
    <div className="mn-canvas-wrap">
      <div className="mn-canvas-main">
        <div className="mn-canvasbar" role="toolbar" aria-label="画布工具">
          <label className="mn-canvas-filter">项目
            <select value={state.project} onChange={e => onStateChange({ project: e.target.value, node: null })}>
              <option value="">全部可见项目</option>
              {(graph.projects ?? []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <div className="mn-segment" role="group" aria-label="显示方式">
            <button type="button" className={`wb-button${state.mode === 'canvas' ? ' wb-button-primary' : ''}`} aria-pressed={state.mode === 'canvas'} onClick={() => onStateChange({ mode: 'canvas' })}>画布</button>
            <button type="button" className={`wb-button${state.mode === 'list' ? ' wb-button-primary' : ''}`} aria-pressed={state.mode === 'list'} onClick={() => onStateChange({ mode: 'list' })}>列表</button>
          </div>
          <label className="wb-checkbox mn-collapse"><input type="checkbox" checked={collapsed} onChange={e => onStateChange({ collapsed: e.target.checked })} />折叠方案/检查/交付</label>
          {state.mode === 'canvas' && <>
            <button type="button" className="wb-button" aria-label="缩小画布" onClick={() => zoomBy(1 / 1.2)}>−</button>
            <span className="mn-zoom-readout" aria-live="polite">{Math.round(scale * 100)}%</span>
            <button type="button" className="wb-button" aria-label="放大画布" onClick={() => zoomBy(1.2)}>＋</button>
            <button type="button" className="wb-button" onClick={fitView}>适应视图</button>
            <button type="button" className="wb-button" disabled={!selected || !nodeById.has(selected.id)} onClick={locateSelected}>定位选中</button>
          </>}
        </div>
        <div className="mn-legend" aria-label="图例">
          {(Object.keys(TONE_LABEL) as NodeTone[]).map(t => <span key={t} className={`mn-legend-item mn-tone-${t}`}><i aria-hidden="true" />{TONE_LABEL[t]}</span>)}
          <span className="mn-legend-item"><i className="mn-legend-dash" aria-hidden="true" />虚线：目标或卡点，不是已发生的交付</span>
        </div>
        {graph.truncated && <p className="mn-hint">任务较多，只显示最近的部分；请按项目筛选查看完整链路。</p>}
        {dense && state.collapsed === null && <p className="mn-hint">对象较多，已折叠方案/检查/交付阶段；选择项目或取消折叠可展开。</p>}

        {shown.nodes.length === 0
          ? <EmptyState title="暂无可投影的对象" description="代码库、需求或任务出现后会生成工作关系图。" />
          : state.mode === 'list'
            ? <GraphList graph={shown} selectedId={state.node} onSelect={select} />
            : (
              <div className="mn-canvas" ref={canvasRef} tabIndex={0} role="application"
                aria-label="工程关系画布。方向键平移，加减号缩放，0 适应视图；Tab 可逐个选中节点"
                onWheel={handleWheel} onPointerDown={handlePointerDown} onPointerMove={handlePointerMove}
                onPointerUp={handlePointerUp} onPointerCancel={handlePointerUp} onKeyDown={handleKey}>
                <div className="mn-world" style={{ width: worldWidth, height: worldHeight, transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})` }}>
                  <svg width={worldWidth} height={worldHeight} aria-hidden="true">
                    {shown.edges.map((edge, i) => {
                      const from = nodeById.get(edge.from)
                      const to = nodeById.get(edge.to)
                      if (!from || !to) return null
                      return <path key={`${edge.from}-${edge.to}-${i}`} d={edgePath(from, to)} className={`mn-edge mn-edge-${edge.kind}`}>
                        <title>{EDGE_LABEL[edge.kind]}</title>
                      </path>
                    })}
                  </svg>
                  {laidOutNodes.map(node => (
                    <button type="button" key={node.id}
                      className={`mn-node mn-node-${node.type} mn-tone-${node.tone ?? 'pending'}${state.node === node.id ? ' is-selected' : ''}`}
                      style={{ left: node.x, top: node.y, width: NODE_WIDTH, height: NODE_HEIGHT }}
                      aria-pressed={state.node === node.id}
                      aria-label={`${TYPE_LABEL[node.type]}：${node.full_label ?? node.label}，${node.sublabel}`}
                      onClick={() => select(node.id)}>
                      <span className="mn-node-type">{TYPE_LABEL[node.type]}</span>
                      <span className="mn-node-label" title={node.full_label ?? node.label}>{node.label}</span>
                      <small title={node.sublabel}>{node.type === 'target' ? TARGET_NOTE : node.sublabel}</small>
                    </button>
                  ))}
                </div>
              </div>
            )}
      </div>
      <aside className="mn-canvas-side" aria-label="节点详情">
        {selected
          ? <NodeDetail node={selected} />
          : <EmptyState title="选择一个节点" description="这里会显示它的事实、来源 ID 和可进入的真实详情页。缺少的事实会写明“未记录”。" />}
      </aside>
    </div>
  )
}
