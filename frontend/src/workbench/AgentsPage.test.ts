import { describe, expect, it } from 'vitest'

import { feedbackStatusLabel, artifactLinks, draftCanApply, isTerminalRun, unwrapAgentMessageResponse } from './AgentsPage'

describe('职能体聊天接口映射', () => {
  it('解包发送接口的 conversation/run/draft envelope', () => {
    const result = unwrapAgentMessageResponse({ conversation: { id: 'c1', agent_id: 'a1', mode: 'do', messages: [] }, run: { id: 'r1', project_id: 'p1', request: 'x', revision: 1, status: 'queued', created_at: '', updated_at: '' }, needs_project: false })
    expect(result.conversation?.id).toBe('c1')
    expect(result.run?.status).toBe('queued')
    expect(result.needsProject).toBe(false)
  })

  it('没有 conversation 时明确标记为空，并保留待绑定项目状态', () => {
    const result = unwrapAgentMessageResponse({ needs_project: true })
    expect(result.conversation).toBeNull()
    expect(result.run).toBeNull()
    expect(result.needsProject).toBe(true)
  })

  it('区分运行中的 pending 与终态，并从 object artifacts 提取链接', () => {
    expect(isTerminalRun('running')).toBe(false)
    expect(isTerminalRun('published')).toBe(true)
    expect(artifactLinks({ pr_url: 'https://example.test/pr/1', commit: 'abc' })).toEqual([{ label: 'pr_url', href: 'https://example.test/pr/1' }])
  })

  it('只有有修订且没有冲突的草稿可以应用', () => {
    expect(draftCanApply({ revision: 0, conflicts: [] })).toBe(false)
    expect(draftCanApply({ revision: 2, conflicts: ['版本冲突'] })).toBe(false)
    expect(draftCanApply({ revision: 2, conflicts: [] })).toBe(true)
  })
})


it('反馈状态仅依据持久化采用状态展示，不把待处理反馈称为已执行', () => {
  expect(feedbackStatusLabel('pending')).toContain('自动接续')
  expect(feedbackStatusLabel('pending')).toContain('人工处理')
  expect(feedbackStatusLabel('adopted')).toContain('已纳入')
  expect(feedbackStatusLabel()).toBeNull()
})
