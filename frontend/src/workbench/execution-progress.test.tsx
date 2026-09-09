import { describe, it, expect } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ExecutionProgress } from './RunPage'
import type { Run } from '../workspace/types'

describe('execution progress', () => {
  it('shows server capacity and dependency waits separately', () => {
    const run = { plan: { tasks: [{ id: 'a', title: '计算模块' }, { id: 'b', title: '页面', depends_on: ['a'] }] }, tasks: [{ id: 'a', status: 'running', activity: { phase: 'waiting_capacity' } }, { id: 'b', status: 'pending' }] } as unknown as Run
    const html = renderToStaticMarkup(<ExecutionProgress run={run} />)
    expect(html).toContain('等待服务器执行空位')
    expect(html).toContain('等待前置任务：a')
  })
  it('does not display old activity as current after completion', () => {
    const run = { plan: { tasks: [{ id: 'a', title: '计算模块' }] }, tasks: [{ id: 'a', status: 'verified', activity: { phase: 'command' } }] } as unknown as Run
    const html = renderToStaticMarkup(<ExecutionProgress run={run} />)
    expect(html).not.toContain('执行项目命令')
    expect(html).toContain('已验证')
  })
})

it('持续编码使用单任务进度，并明确自动恢复连接而非等待审批', () => {
  const run = { execution_mode: 'continuous', plan: { tasks: [{ id: 'coding', title: '维护转换器' }] }, tasks: [{ id: 'coding', status: 'running', activity: { phase: 'reconnecting' } }] } as unknown as Run
  const html = renderToStaticMarkup(<ExecutionProgress run={run} />)
  expect(html).toContain('持续编码进度')
  expect(html).toContain('正在自动恢复')
  expect(html).not.toContain('独立任务并行')
  expect(html).not.toContain('审批')
})

it('持续编码的 checks 活动显示真实检查，不提前宣告验证通过', () => {
  const run = { execution_mode: 'continuous', plan: { tasks: [{ id: 'coding', title: '维护转换器' }] }, tasks: [{ id: 'coding', status: 'running', activity: { phase: 'checks' } }] } as unknown as Run
  const html = renderToStaticMarkup(<ExecutionProgress run={run} />)
  expect(html).toContain('运行项目检查')
  expect(html).not.toContain('已验证')
})
