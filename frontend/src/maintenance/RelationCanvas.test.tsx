// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'

import RelationCanvas, { collapseStages } from './RelationCanvas'
import type { CanvasState } from './RelationCanvas'
import { MaintenanceBaseContext } from './base-path'
import type { Graph, GraphNode } from './types'

afterEach(cleanup)

function sampleGraph(overrides: Partial<Graph> = {}): Graph {
  return {
    generated_at: '2026-09-23T02:00:00Z',
    nodes: [
      { id: 'repo:proj-1', type: 'repo', label: '企业报表系统', sublabel: '维护代码库', status: 'ready', task_id: null, project_id: 'proj-1',
        links: [{ kind: 'repo', id: 'proj-1', label: '仓库详情' }] },
      { id: 'req:req-1', type: 'requirement', label: '金额汇总差异', sublabel: '人工提交', status: 'dispatched', task_id: null, project_id: 'proj-1' },
      { id: 'task:task-1', type: 'task', label: '修复方案', sublabel: '执行中', status: 'running', task_id: 'task-1', project_id: 'proj-1',
        facts: [{ label: '执行发起人', value: 'owner' }, { label: '负责人', value: '未记录（系统没有该字段）' }],
        links: [{ kind: 'task', id: 'task-1', label: '任务现场' }] },
      { id: 'target:task-1', type: 'target', label: '目标：补丁', sublabel: '待生成', status: 'pending', task_id: 'task-1', project_id: 'proj-1' },
    ],
    edges: [
      { from: 'repo:proj-1', to: 'req:req-1', kind: 'has' },
      { from: 'req:req-1', to: 'task:task-1', kind: 'creates' },
      { from: 'task:task-1', to: 'target:task-1', kind: 'targets' },
    ],
    truncated: false,
    ...overrides,
  }
}

function renderCanvas(graph: Graph, initial: Partial<CanvasState> = {}, base = '/maintenance') {
  const onChange = vi.fn()
  function Harness() {
    const [state, setState] = useState<CanvasState>({ mode: 'canvas', project: '', node: null, collapsed: null, ...initial })
    return <MaintenanceBaseContext.Provider value={base}><MemoryRouter>
      <RelationCanvas graph={graph} state={state} onStateChange={next => { onChange(next); setState(s => ({ ...s, ...next })) }} />
    </MemoryRouter></MaintenanceBaseContext.Provider>
  }
  render(<Harness />)
  return onChange
}

describe('RelationCanvas 工作画布', () => {
  it('按图数据渲染节点', () => {
    renderCanvas(sampleGraph())
    expect(screen.getByText('企业报表系统')).toBeTruthy()
    expect(screen.getByText('金额汇总差异')).toBeTruthy()
    expect(screen.getByText('修复方案')).toBeTruthy()
  })

  it('目标节点显示未生成不计入交付的说明', () => {
    renderCanvas(sampleGraph())
    expect(screen.getByText('目标，尚未生成，不计入交付')).toBeTruthy()
  })

  it('点击节点后显示事实与真实入口链接，并把选中写回状态', () => {
    const onChange = renderCanvas(sampleGraph(), {}, '/embed/maintenance')
    fireEvent.click(screen.getByText('修复方案'))
    expect(onChange).toHaveBeenCalledWith({ node: 'task:task-1' })
    expect(screen.getByText('执行发起人')).toBeTruthy()
    expect(screen.getByText('未记录（系统没有该字段）')).toBeTruthy()
    expect(screen.getByRole('link', { name: '任务现场' }).getAttribute('href')).toBe('/embed/maintenance/task-1')
  })

  it('节点没有入口时不显示跳转，代码库节点跳到仓库详情', () => {
    renderCanvas(sampleGraph())
    fireEvent.click(screen.getByText('目标，尚未生成，不计入交付'))
    expect(screen.getByText('这个节点没有可进入的详情页。')).toBeTruthy()
    fireEvent.click(screen.getByText('企业报表系统'))
    expect(screen.getByRole('link', { name: '仓库详情' }).getAttribute('href')).toBe('/maintenance/repos/proj-1')
    expect(screen.queryByRole('link', { name: '任务现场' })).toBeNull()
  })

  it('提供适应视图与定位选中；未选中时定位不可用', () => {
    renderCanvas(sampleGraph())
    expect(screen.getByRole('button', { name: '适应视图' })).toBeTruthy()
    expect((screen.getByRole('button', { name: '定位选中' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByText('修复方案'))
    expect((screen.getByRole('button', { name: '定位选中' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('没有节点时显示空状态', () => {
    renderCanvas(sampleGraph({ nodes: [], edges: [] }))
    expect(screen.getByText('暂无可投影的对象')).toBeTruthy()
  })

  it('truncated 时提示只显示部分并建议按项目筛选', () => {
    renderCanvas(sampleGraph({ truncated: true }))
    expect(screen.getByText('任务较多，只显示最近的部分；请按项目筛选查看完整链路。')).toBeTruthy()
  })

  it('列表视图提供同一份投影，不靠拖拽也能选中', () => {
    renderCanvas(sampleGraph(), { mode: 'list' })
    const list = screen.getByRole('list', { name: '关系列表' })
    expect(list.querySelectorAll('button').length).toBe(4)
    fireEvent.click(screen.getByRole('button', { name: /修复方案/ }))
    expect(screen.getByRole('link', { name: '任务现场' })).toBeTruthy()
  })

  it('项目筛选只提供服务端给出的范围内项目，并把选择交回状态', () => {
    const onChange = renderCanvas(sampleGraph({ projects: [{ id: 'proj-1', name: '企业报表系统' }] }))
    const select = screen.getByLabelText('项目') as HTMLSelectElement
    expect([...select.options].map(o => o.value)).toEqual(['', 'proj-1'])
    fireEvent.change(select, { target: { value: 'proj-1' } })
    expect(onChange).toHaveBeenCalledWith({ project: 'proj-1', node: null })
  })

  it('折叠阶段时隐藏方案/检查/交付，阻塞改挂到任务上', () => {
    const graph = sampleGraph({
      nodes: [...sampleGraph().nodes,
        { id: 'plan:task-1', type: 'plan', label: '方案A', sublabel: '等待批准', status: 'awaiting_approval', task_id: 'task-1', project_id: 'proj-1', tone: 'attention' },
        { id: 'blocker:task-1', type: 'blocker', label: '等待批准', sublabel: '请批准', status: 'approval', task_id: 'task-1', project_id: 'proj-1', tone: 'attention' }],
      edges: [...sampleGraph().edges, { from: 'task:task-1', to: 'plan:task-1', kind: 'plans' }, { from: 'plan:task-1', to: 'blocker:task-1', kind: 'blocked_by' }],
    })
    const collapsed = collapseStages(graph)
    expect(collapsed.nodes.map(n => n.id)).not.toContain('plan:task-1')
    expect(collapsed.edges).toContainEqual({ from: 'task:task-1', to: 'blocker:task-1', kind: 'blocked_by' })
    renderCanvas(graph, { collapsed: true })
    expect(screen.queryByText('方案A')).toBeNull()
    expect(screen.getByText('请批准')).toBeTruthy()
  })
})

describe('layout', () => {
  it('reserves one row per node in a task group so a multi-artifact task never shares the next task\'s row', async () => {
    const { layout } = await import('./RelationCanvas')
    const node = (id: string, type: GraphNode['type']): GraphNode =>
      ({ id, type, label: id, sublabel: '', status: '', task_id: null, project_id: 'p' })
    const nodes = [node('repo:p', 'repo'), node('req:1', 'requirement'), node('req:2', 'requirement'),
      node('task:1', 'task'), node('task:2', 'task'),
      node('artifact:1:a', 'artifact'), node('artifact:1:b', 'artifact'),
      node('target:2', 'target'), node('blocker:2', 'blocker')]
    const edges: Graph['edges'] = [
      { from: 'repo:p', to: 'req:1', kind: 'has' }, { from: 'repo:p', to: 'req:2', kind: 'has' },
      { from: 'req:1', to: 'task:1', kind: 'creates' }, { from: 'req:2', to: 'task:2', kind: 'creates' },
      { from: 'task:1', to: 'artifact:1:a', kind: 'produces' }, { from: 'task:1', to: 'artifact:1:b', kind: 'produces' },
      { from: 'task:2', to: 'target:2', kind: 'targets' }, { from: 'task:2', to: 'blocker:2', kind: 'blocked_by' }]
    const y = new Map(layout(nodes, edges).nodes.map(n => [n.id, n.y]))
    // Task 2's group starts below both of task 1's artifacts.
    expect(y.get('task:2')!).toBeGreaterThan(y.get('artifact:1:b')!)
    expect(y.get('target:2')).toBe(y.get('task:2'))
    expect(y.get('artifact:1:a')).toBe(y.get('task:1'))
    const ys = [...y.values()]
    const cols = layout(nodes, edges).nodes.map(n => `${n.x}:${n.y}`)
    expect(new Set(cols).size).toBe(cols.length) // no two nodes on the same spot
    expect(ys.length).toBe(nodes.length)
  })
})
