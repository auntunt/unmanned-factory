import { describe, expect, it } from 'vitest'
import { canGenerateNextPlan, frozenPolicy, runEvidence, runGuidance, runView } from './run-guidance'
import type { Run } from '../workspace/types'

function run(overrides: Partial<Run> = {}): Run {
  return { id: 'r 1', project_id: 'p1', request: 'ship it', status: 'needs_human', revision: 1, created_at: 'x', updated_at: 'x', ...overrides }
}

const stoppedCost = 'observed provider cost $3.9453 exceeds budget $3.0000'

describe('runGuidance', () => {
  it('does not invent a question for a paused run without triage questions', () => {
    const guidance = runGuidance(run({ artifacts: { needs_human: 'manual intervention requested' } }))
    expect(guidance.kind).toBe('paused')
    expect(guidance.label).toBe('运行已暂停')
    expect(guidance.summary).not.toContain('回答')
  })

  it('separates recorded requirements, plan approval, budget, billing and recovery', () => {
    expect(runGuidance(run({ status: 'needs_clarification', triage: { decision: 'needs_clarification', reasons: [], questions: ['生产环境地址？'], risk: 'low' } })).kind).toBe('requirements')
    expect(runGuidance(run({ status: 'awaiting_approval' })).kind).toBe('approval')
    expect(runGuidance(run({ status: 'awaiting_approval', plan: { title: 'p', summary: '', questions: ['接口地址？'], tasks: [] } })).kind).toBe('requirements')
    expect(runGuidance(run({ triage: { decision: 'needs_clarification', reasons: [], questions: ['无关问题'], risk: 'low' }, artifacts: { needs_human: stoppedCost } })).kind).toBe('budget')
    expect(runGuidance(run({ artifacts: { billing_incomplete: 'provider usage unknown' } })).summary).toContain('美元费用')
    expect(runGuidance(run({ artifacts: { needs_human: 'recovery paused after interruption' } })).kind).toBe('recovery')
  })

  it('keeps historic budget data out of terminal-run guidance and favors run.error for a failure', () => {
    expect(runGuidance(run({ status: 'published', artifacts: { needs_human: stoppedCost } })).kind).toBe('delivery')
    expect(runGuidance(run({ status: 'cancelled', artifacts: { needs_human: stoppedCost } })).kind).toBe('progress')
    expect(runGuidance(run({ status: 'discarded', artifacts: { needs_human: stoppedCost } })).label).toBe('任务已废弃')
    const failed = Object.assign(run({ status: 'failed', artifacts: { needs_human: 'triage fallback' } }), { error: 'actual runner error' })
    const guidance = runGuidance(failed)
    expect(guidance.rawEvidence).toBe('actual runner error')
  })

  it('only marks persisted plan, task, check, and delivery facts as evidence', () => {
    expect(runEvidence(run({ status: 'failed', request: '', plan: null })).plan).toBe(false)
    expect(runEvidence(run({ status: 'failed', request: '', plan: null })).execution).toBe(false)
    expect(runEvidence(run({ tasks: [{ id: 't1', attempts: [{ checks: [{ name: 'unit', outcome: 'failed', exit: 0 }] }] }] })).checks).toBe('failed')
    expect(runEvidence(run({ artifacts: { checks: [{ name: 'unit', outcome: 'passed' }], commit: 'abc' } })).checks).toBe('passed')
    expect(runEvidence(run({ artifacts: { checks: [{ name: 'unit', outcome: 'passed' }], commit: 'abc' } })).delivery).toBe(true)
  })

  it('uses the frozen policy for auto waiting guidance and excludes budget stops from new-plan input', () => {
    const autonomous = Object.assign(run({ status: 'awaiting_approval', triage: { decision: 'human_approval', reasons: ['风险超出项目自动策略'], questions: [], risk: 'high' } }), { policy: { revision: 7, mode: 'autonomous' } })
    expect(frozenPolicy(autonomous)).toEqual({ revision: 7, mode: 'autonomous' })
    expect(runGuidance(autonomous).summary).toContain('风险超出项目自动策略')
    expect(canGenerateNextPlan(autonomous, true)).toBe(true)
    expect(canGenerateNextPlan(run({ artifacts: { needs_human: stoppedCost } }), true)).toBe(false)
  })

  it('creates stable run view links and uses an explicit fallback for old links', () => {
    expect(runGuidance(run({ status: 'running' })).primaryHref).toBe('/runs/r%201?view=execution')
    expect(runView('verification')).toBe('verification')
    expect(runView(null, 'execution')).toBe('execution')
    expect(runView('events')).toBe('requirements')
  })
})
