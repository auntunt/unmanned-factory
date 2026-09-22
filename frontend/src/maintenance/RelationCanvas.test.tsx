// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import RelationCanvas from './RelationCanvas'
import type { Graph } from './types'

afterEach(cleanup)

function sampleGraph(overrides: Partial<Graph> = {}): Graph {
  return {
    generated_at: '2026-09-23T02:00:00Z',
    nodes: [
      { id: 'repo:proj-1', type: 'repo', label: '企业报表系统', sublabel: '维护代码库', status: 'ready', task_id: null, project_id: 'proj-1' },
      { id: 'req:req-1', type: 'requirement', label: '金额汇总差异', sublabel: '人工提交', status: 'dispatched', task_id: null, project_id: 'proj-1' },
      { id: 'task:task-1', type: 'task', label: '修复方案', sublabel: '执行中', status: 'running', task_id: 'task-1', project_id: 'proj-1' },
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

describe('RelationCanvas 工作画布', () => {
  it('按图数据渲染节点', () => {
    render(<RelationCanvas graph={sampleGraph()} onOpenTask={vi.fn()} />)
    expect(screen.getByText('企业报表系统')).toBeTruthy()
    expect(screen.getByText('金额汇总差异')).toBeTruthy()
    expect(screen.getByText('修复方案')).toBeTruthy()
  })

  it('目标节点显示未生成不计入交付的说明', () => {
    render(<RelationCanvas graph={sampleGraph()} onOpenTask={vi.fn()} />)
    expect(screen.getByText('目标，尚未生成，不计入交付')).toBeTruthy()
  })

  it('点击节点后显示详情，进入任务现场触发 onOpenTask', () => {
    const onOpenTask = vi.fn()
    render(<RelationCanvas graph={sampleGraph()} onOpenTask={onOpenTask} />)
    fireEvent.click(screen.getByText('修复方案'))
    expect(screen.getByText('进入任务现场')).toBeTruthy()
    fireEvent.click(screen.getByText('进入任务现场'))
    expect(onOpenTask).toHaveBeenCalledWith('task-1')
  })

  it('节点没有 task_id 时不显示进入任务现场按钮', () => {
    render(<RelationCanvas graph={sampleGraph()} onOpenTask={vi.fn()} />)
    fireEvent.click(screen.getByText('企业报表系统'))
    expect(screen.queryByText('进入任务现场')).toBeNull()
  })

  it('提供适应视图按钮', () => {
    render(<RelationCanvas graph={sampleGraph()} onOpenTask={vi.fn()} />)
    expect(screen.getByRole('button', { name: '适应视图' })).toBeTruthy()
  })

  it('没有节点时显示空状态', () => {
    render(<RelationCanvas graph={sampleGraph({ nodes: [], edges: [] })} onOpenTask={vi.fn()} />)
    expect(screen.getByText('暂无可投影的对象')).toBeTruthy()
  })

  it('truncated 时提示已聚合显示部分', () => {
    render(<RelationCanvas graph={sampleGraph({ truncated: true })} onOpenTask={vi.fn()} />)
    expect(screen.getByText('对象较多，已按项目聚合显示部分。')).toBeTruthy()
  })
})
