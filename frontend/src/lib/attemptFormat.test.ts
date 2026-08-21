import { describe, expect, it } from 'vitest'
import {
  fmtCost,
  fmtDuration,
  resCode,
  resColor,
  shortRole,
  summarize,
  verdictSummary,
} from './attemptFormat'
import type { Attempt } from '../types'

function mkAttempt(over: Partial<Attempt> = {}): Attempt {
  return {
    attempt_no: 1,
    model: 'haiku',
    resolution: 'reworked',
    cost_usd: 0,
    wall_clock_s: 0,
    tokens_in: 0,
    tokens_out: 0,
    created_at: '2026-08-20T10:00:00Z',
    harness: 'claude',
    harness_version: '1.0',
    commit: null,
    verdicts: [],
    ...over,
  } as Attempt
}

describe('fmtCost / fmtDuration 的诚实性', () => {
  // 未派工轮次 cost=0。显示 $0.00 会被读成「跑了而且免费」，
  // 必须显示 — 表示「没这个数」。
  it('零花费显示 — 而不是 $0.00', () => {
    expect(fmtCost(0)).toBe('—')
  })

  it('零耗时显示 — 而不是 0s', () => {
    expect(fmtDuration(0)).toBe('—')
  })

  it('真实花费保留两位小数', () => {
    expect(fmtCost(2.373)).toBe('$2.37')
  })

  it('耗时超过一分钟拆成 m+s', () => {
    expect(fmtDuration(45)).toBe('45s')
    expect(fmtDuration(120)).toBe('2m')
    expect(fmtDuration(704)).toBe('11m44s')
  })
})

describe('resCode / resColor', () => {
  it('已登记的 resolution 转成短代码', () => {
    expect(resCode('merged')).toBe('OK')
    expect(resCode('not_dispatched')).toBe('SKIP')
    expect(resCode('escalated')).toBe('ESCALATE')
  })

  // 没登记过的状态原样大写显示，不吞成 UNKNOWN——
  // 操作者至少还能拿这个串去审计库搜。
  it('未登记的 resolution 保留原值而不是 UNKNOWN', () => {
    expect(resCode('some_new_state')).toBe('SOME_NEW_STATE')
  })

  it('未登记的 resolution 不套用成功/失败配色', () => {
    expect(resColor('some_new_state')).toBe('text-slate-600')
    expect(resColor('merged')).toBe('text-emerald-700')
  })
})

describe('verdictSummary / shortRole', () => {
  it('去掉「监工」二字塞进窄列', () => {
    expect(shortRole('risk')).toBe('风险')
    expect(shortRole('regression')).toBe('回归')
  })

  it('pass 用 ✓，fail 用 ✗', () => {
    const a = mkAttempt({
      verdicts: [
        { role: 'risk', verdict: 'pass', claims: [] },
        { role: 'regression', verdict: 'fail', claims: [] },
      ],
    } as Partial<Attempt>)
    expect(verdictSummary(a)).toBe('✓风险 ✗回归')
  })
})

describe('summarize', () => {
  it('executed 只数真实花过钱的轮次', () => {
    const s = summarize([
      mkAttempt({ attempt_no: 1, cost_usd: 0, resolution: 'not_dispatched' }),
      mkAttempt({ attempt_no: 2, cost_usd: 1.5, wall_clock_s: 60 }),
    ])
    expect(s.total).toBe(2)
    expect(s.executed).toBe(1)
    expect(s.cost).toBeCloseTo(1.5)
    expect(s.wall).toBe(60)
  })

  it('failedRounds 数含 fail 判词的轮次', () => {
    const s = summarize([
      mkAttempt({ attempt_no: 1, verdicts: [{ role: 'risk', verdict: 'fail', claims: [] }] } as Partial<Attempt>),
      mkAttempt({ attempt_no: 2, verdicts: [{ role: 'risk', verdict: 'pass', claims: [] }] } as Partial<Attempt>),
    ])
    expect(s.failedRounds).toBe(1)
  })
})
