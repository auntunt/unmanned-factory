import { describe, expect, it } from 'vitest'
import { planVersions, type PlanVersion } from './PlanVersions'
import type { Run } from '../workspace/types'

const run: Run = { id: 'r1', project_id: 'p1', request: 'request', status: 'awaiting_approval', revision: 3, created_at: 'x', updated_at: 'now', plan: { title: 'current', summary: 'current summary', questions: [], tasks: [] } }
const version = (revision: number, summary: string): PlanVersion => ({ revision, summary, plan: { title: `plan ${revision}`, summary, questions: [], tasks: [] }, at: `2026-01-0${revision}` })

describe('planVersions', () => {
  it('uses the dedicated saved versions, preserves the current plan, and de-duplicates revisions', () => {
    const versions = planVersions(run, [version(1, 'first'), version(3, 'saved current'), version(1, 'replacement')])
    expect(versions.map((item) => item.revision)).toEqual([3, 1])
    expect(versions[1].summary).toBe('replacement')
    expect(versions[0].summary).toBe('saved current')
  })
  it('uses the current in-memory plan only when a saved version is unavailable', () => {
    expect(planVersions(run, []).map((item) => item.revision)).toEqual([3])
  })
})
