import { describe, expect, it } from 'vitest'

import { taskGroupCounts } from './OverviewPage'
import { validateChecks } from './ChecksEditor'
import { formatDate, statusLabel } from './ui'

describe('工作台 shell helpers', () => {
  it('用中文显示主要运行状态，未知值仍保留原值', () => {
    expect(statusLabel('needs_clarification')).toBe('需要确认')
    expect(statusLabel('future_state')).toBe('future_state')
  })

  it('日期格式稳定且不把无效日期伪装成时间', () => {
    expect(formatDate('2026-09-08T12:30:00Z')).toMatch(/2026/)
    expect(formatDate('not-a-date')).toBe('not-a-date')
    expect(formatDate()).toBe('—')
  })

  it('任务分组只统计真实任务，不凭空补全指标', () => {
    const result = taskGroupCounts([
      { id: 'r1', project_id: 'p1', request: 'x', status: 'running', revision: 1, created_at: 'x', updated_at: 'x', tasks: [{ status: 'running' }, { status: 'completed' }, { status: 'failed' }] },
      { id: 'r2', project_id: 'p1', request: 'x', status: 'planning', revision: 1, created_at: 'x', updated_at: 'x' },
    ])
    expect(result).toEqual({ total: 3, active: 1, completed: 1, blocked: 1 })
  })

  it('检查编辑器保留参数原文，并允许暂存空检查', () => {
    expect(validateChecks({})).toBeNull()
    expect(validateChecks({ test: ['pytest', ' -k "slow path" '] })).toBeNull()
    expect(validateChecks({ 'bad name': ['pytest'] })).toContain('检查名称')
    expect(validateChecks({ test: [''] })).toContain('程序')
  })
})
