// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import RecordedChecks, { recordedChecks } from './RecordedChecks'
import type { Run } from '../workspace/types'
afterEach(cleanup)
const run = (artifacts: unknown, tasks?: unknown) => ({ artifacts, tasks } as Run)

it('renders final success and historical failure without inventing acceptance-ledger totals', () => {
  const rows = recordedChecks(run({
    checks: [{ name: 'welcome', exit: 0, argv: ['python', '-c', 'assert ready'], stdout: '', stderr: '', duration_s: .024 }],
    tasks: [{ id: 'welcome', attempts: [{ attempt: 1, checks: [{ name: 'welcome', exit: 1, stderr: 'AssertionError: 未达到验收条件' }] }] }],
  }))
  const { container } = render(<RecordedChecks rows={rows} />)
  expect(screen.getByText('集成检查 · welcome')).toBeTruthy()
  expect(screen.getByText('通过')).toBeTruthy()
  expect(screen.getByText('未通过')).toBeTruthy()
  expect(screen.getByText('退出码：0')).toBeTruthy()
  expect(screen.getByText('退出码：1')).toBeTruthy()
  expect(screen.getByText('AssertionError: 未达到验收条件')).toBeTruthy()
  expect(screen.getByLabelText('检查命令参数').textContent).toContain('assert ready')
  expect(container.querySelector('.cv-verify-counts')).toBeNull()
})
it('never treats missing exit, timed-out or cancelled checks as passing', () => {
  const rows = recordedChecks(run({ checks: [
    { name: 'unknown' }, { name: 'timeout', exit: 0, timeout: true }, { name: 'cancel', exit: 0, cancelled: true },
  ] }))
  render(<RecordedChecks rows={rows} />)
  expect(screen.getByText('未验证')).toBeTruthy()
  expect(screen.getByText('超时')).toBeTruthy()
  expect(screen.getByText('已取消')).toBeTruthy()
  expect(screen.queryByText('通过')).toBeNull()
})
it('uses persisted artifact task evidence once instead of duplicating the live task projection', () => {
  const tasks = [{ id: 't', checks: [{ name: 'check', exit: 0 }] }]
  expect(recordedChecks(run({ tasks }, tasks))).toHaveLength(1)
  expect(recordedChecks(run({}, tasks))).toHaveLength(1)
})
it('ignores malformed evidence and does not fabricate a check', () => {
  expect(recordedChecks(run({ checks: [null, false, 'pass'], tasks: [null, 3] }))).toEqual([])
  const { container } = render(<RecordedChecks rows={[]} />)
  expect(container.textContent).toBe('')
})
