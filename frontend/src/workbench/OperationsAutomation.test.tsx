// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import OperationsSettings from './OperationsSettings'
import ProjectInspection from './ProjectInspection'
import { request } from '../workspace/api'
vi.mock('../workspace/api', () => ({ request: vi.fn() }))
const api = vi.mocked(request)
const unauthorized = vi.fn()
afterEach(() => { cleanup(); api.mockReset() })

it('configures optional notification and knowledge switches without echoing the secret', async () => {
  api.mockResolvedValueOnce({ revision: 0, webhook_configured: false, knowledge_enabled: false } as never)
  render(<OperationsSettings csrfToken="csrf" onUnauthorized={unauthorized} />)
  const webhook = await screen.findByLabelText('飞书机器人 webhook')
  expect(webhook.getAttribute('type')).toBe('password')
  fireEvent.change(webhook, { target: { value: 'https://open.feishu.cn/open-apis/bot/v2/hook/test' } })
  fireEvent.click(screen.getByLabelText('回流并复用已验证的运维事实'))
  api.mockResolvedValueOnce({ revision: 1, webhook_configured: true, knowledge_enabled: true } as never)
  fireEvent.click(screen.getByText('保存运维设置'))
  await screen.findByText('运维设置已保存。')
  expect(api.mock.calls[1][1]?.body).toMatchObject({ revision: 0, knowledge_enabled: true, webhook: 'https://open.feishu.cn/open-apis/bot/v2/hook/test' })
  expect((webhook as HTMLInputElement).value).toBe('')
  api.mockResolvedValueOnce({ revision: 2, webhook_configured: false, knowledge_enabled: true } as never)
  fireEvent.click(screen.getByText('关闭告警通知'))
  await waitFor(() => expect(api.mock.calls[2][1]?.body).toMatchObject({ webhook: '', knowledge_enabled: true }))
})

it('shows inspection trend and removes a sourced fact using its revision', async () => {
  api.mockImplementation(async (url, options) => {
    if (options?.method === 'PUT') return {} as never
    if (url.endsWith('/inspection')) return { enabled: false, revision: 0, interval_s: 3600, consecutive_failures: 3,
      history: [{ run_id: 'r1', at: '2026-09-14T01:00:00Z', verdict: 'unverified', duration_s: 1.5 }] } as never
    return { entries: [{ key: 'operation.startup_command', revision: 2, kind: 'fact', status: 'active',
      title: '已验证启动命令', content: 'npm start', paths: [], commit_sha: null,
      provenance: { source: 'verified_operation', run_id: 'r1', at: '2026-09-14T01:00:00Z' } }] } as never
  })
  render(<MemoryRouter><ProjectInspection projectId="p1" csrfToken="csrf" onUnauthorized={unauthorized} isAdmin /></MemoryRouter>)
  await screen.findByText('连续未通过：3 次')
  expect((await screen.findByRole('link', { name: /unverified/ })).getAttribute('href')).toBe('/runs/r1?view=verification')
  await screen.findByText('npm start')
  fireEvent.click(screen.getByText('删除事实'))
  await waitFor(() => expect(screen.queryByText('npm start')).toBeNull())
  expect(api.mock.calls.find(([, options]) => options?.method === 'PUT')?.[1]?.body).toMatchObject({ status: 'retired', expected_revision: 2 })
})
