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
