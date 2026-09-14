// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import OperationResults from './OperationResults'
import type { Run } from '../workspace/types'
afterEach(cleanup)
it('shows evidence and marks missing maintenance facts unverified', () => {
  const run = { source: { operation: 'startup' }, artifacts: { operation_results: { health: { status: 'pass', evidence: 'GET /health returned 200' } } } } as unknown as Run
  render(<OperationResults run={run} />)
  expect(screen.getByText('最终启动命令 · 未验证')).toBeTruthy()
  expect(screen.getByText('GET /health returned 200')).toBeTruthy()
})

it('exposes unverified independently from the paused run status', async () => {
  const { runDisplayStatus, runEvidence } = await import('./run-guidance')
  const run = { request: 'x', status: 'needs_human', artifacts: { verification: { verdict: 'unverified' } } } as unknown as Run
  expect(runDisplayStatus(run)).toBe('unverified')
  expect(runEvidence(run).checks).toBe('unverified')
})
