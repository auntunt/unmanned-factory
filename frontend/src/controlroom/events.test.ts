import { describe, expect, it } from 'vitest'
import { empty, keyOf, liveAttempts, reduce, type RoomState } from './events'

const fold = (evs: Array<Record<string, unknown> & { type: string }>, start: RoomState = empty) =>
  evs.reduce((s, e) => reduce(s, e), start)

describe('控制室 reducer', () => {
  it('snapshot 建立桶、轮次、计数', () => {
    const s = fold([{
      type: 'snapshot', at: 'x',
      tasks: { inbox: ['T-2'], done: ['T-1'] },
      attempts: [{
        task_id: 'T-1', attempt_no: 1, oracle_class: 'B', class_reason: 'r', model: 'opus',
        cost_usd: 0.5, tokens_in: 1, tokens_out: 2, wall_clock_ms: 1000, commit: 'abc',
        resolution: 'merged', resolution_note: '', stage: 'merged', created_at: 'x',
        verdicts: [{ role: 'regression', verdict: 'pass', claims: 0 }],
      }],
    }])
    expect(s.tasks).toEqual({ 'T-2': 'inbox', 'T-1': 'done' })
    expect(s.mergedCount).toBe(1)
    expect(s.totalCost).toBeCloseTo(0.5)
    expect(s.focus).toBe(keyOf('T-1', 1))
    expect(s.attempts[keyOf('T-1', 1)].verdicts.regression?.verdict).toBe('pass')
  })

  it('一轮从 open 到 final 的阶段流转', () => {
    const base = { task_id: 'T-9', attempt_no: 1, at: 'x' }
    let s = fold([
      { type: 'snapshot', at: 'x', tasks: { inbox: ['T-9'] }, attempts: [] },
      { type: 'task.state', ...base, state: 'running' },
      { type: 'attempt.open', ...base, oracle_class: 'A', class_reason: '', model: 'opus' },
    ])
    const k = keyOf('T-9', 1)
    expect(s.attempts[k].stage).toBe('dispatching')
    expect(liveAttempts(s)).toHaveLength(1)
    expect(s.focus).toBe(k)

    s = fold([{ type: 'attempt.line', ...base, kind: 'tool', text: '读 a.py' }], s)
    expect(s.attempts[k].lines).toEqual([{ kind: 'tool', text: '读 a.py' }])

    s = fold([{ type: 'attempt.result', ...base, cost_usd: 0.3, tokens_in: 1, tokens_out: 1, wall_clock_ms: 5000, has_diff: true }], s)
    expect(s.attempts[k].stage).toBe('judging')
    expect(s.totalCost).toBeCloseTo(0.3)

    s = fold([
      { type: 'gate.verdict', ...base, role: 'regression', verdict: 'pass', claims: 0 },
      { type: 'gate.verdict', ...base, role: 'scope', verdict: 'fail', claims: 2 },
      { type: 'attempt.final', ...base, resolution: 'escalated', commit: null, note: '' },
      { type: 'task.state', ...base, state: 'needs_human' },
    ], s)
    expect(s.attempts[k].stage).toBe('escalated')
    expect(s.attempts[k].verdicts.scope).toEqual({ verdict: 'fail', claims: 2 })
    expect(liveAttempts(s)).toHaveLength(0)
    expect(s.focus).toBe(k)
    expect(s.tasks['T-9']).toBe('needs_human')
    expect(s.mergedCount).toBe(0)
  })

  it('焦点跟着最新在跑的轮走，都完了停在最后一轮', () => {
    const s = fold([
      { type: 'snapshot', at: 'x', tasks: {}, attempts: [] },
      { type: 'attempt.open', task_id: 'A', attempt_no: 1, at: 'x', oracle_class: 'A', class_reason: '', model: '' },
      { type: 'attempt.open', task_id: 'B', attempt_no: 1, at: 'x', oracle_class: 'A', class_reason: '', model: '' },
    ])
    expect(s.focus).toBe(keyOf('B', 1))
    const t = fold([{ type: 'attempt.final', task_id: 'B', attempt_no: 1, at: 'x', resolution: 'merged', commit: 'c' }], s)
    expect(t.focus).toBe(keyOf('A', 1))
    expect(t.mergedCount).toBe(1)
  })

  it('现场行封顶 40 条', () => {
    const lines = Array.from({ length: 50 }, (_, i) => ({
      type: 'attempt.line', task_id: 'T', attempt_no: 1, at: 'x', kind: 'text', text: String(i),
    }))
    const s = fold([{ type: 'snapshot', at: 'x', tasks: {}, attempts: [] }, ...lines])
    expect(s.attempts[keyOf('T', 1)].lines).toHaveLength(40)
    expect(s.attempts[keyOf('T', 1)].lines[0].text).toBe('10')
  })

  it('未知事件不改状态', () => {
    const s = fold([{ type: 'snapshot', at: 'x', tasks: {}, attempts: [] }])
    expect(reduce(s, { type: 'nope' })).toBe(s)
  })
})
