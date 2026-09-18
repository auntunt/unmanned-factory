// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import RuntimeReadiness from './RuntimeReadiness'
import { request } from '../workspace/api'

vi.mock('../workspace/api', async (original) => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)

afterEach(cleanup)

it('renders readiness and blockers from member-filtered response', async () => {
  api.mockResolvedValueOnce({
    readiness: { planning: true, execution: false, publishing: false },
    local_readiness: { planning: true, execution: false, publishing: false },
    blockers: ['cheap: codex SDK is not API-compatible', 'GitHub publishing token is not present'],
    configuration_revision: 3,
  } as never)

  render(<RuntimeReadiness csrfToken="csrf" onUnauthorized={vi.fn()} />)
  expect(await screen.findByText('可用性')).toBeTruthy()
  // Readiness rows
  expect(screen.getByText('规划')).toBeTruthy()
  expect(screen.getByText('执行')).toBeTruthy()
  expect(screen.getByText('发布')).toBeTruthy()
  // Blockers rendered from real data
  expect(screen.getByText('cheap: codex SDK is not API-compatible')).toBeTruthy()
  expect(screen.getByText('GitHub publishing token is not present')).toBeTruthy()
})

it('does not render profiles, tools, host, or auth_sources', async () => {
  api.mockResolvedValueOnce({
    readiness: { planning: true, execution: true, publishing: false },
    blockers: [],
    configuration_revision: 1,
  } as never)

  const { container } = render(<RuntimeReadiness csrfToken="csrf" onUnauthorized={vi.fn()} />)
  await screen.findByText('可用性')
  const html = container.innerHTML
  // These admin-only sections must not appear
  expect(html).not.toContain('模型角色')
  expect(html).not.toContain('运行环境检查')
  expect(html).not.toContain('Python')
  expect(html).not.toContain('auth_sources')
})

it('shows empty state when API returns nothing', async () => {
  api.mockRejectedValueOnce(new Error('network fail'))
  render(<RuntimeReadiness csrfToken="csrf" onUnauthorized={vi.fn()} />)
  expect(await screen.findByText('请求没有完成')).toBeTruthy()
})
