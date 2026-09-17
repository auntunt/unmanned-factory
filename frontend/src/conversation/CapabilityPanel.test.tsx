// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import CapabilityPanel from './CapabilityPanel'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()

const binding = {
  id: 'b1', agent_id: 'a1', pack_id: 'p1', pack_name: 'CSV 清单 → XML', version_id: 'v1', version: 1,
  revision: 1, latest_version: 1, upgrade_available: false, content_digest: 'c'.repeat(64),
  environment: { status: 'ready' as const },
}
const task = (over: Record<string, unknown> = {}) => ({
  id: 't1', pack_id: 'p1', agent_id: 'a1', status: 'succeeded', validation_status: 'passed',
  outputs: [{ id: 'o1', name: 'quote.xml', size: 680, validation_status: 'passed' }], inputs: [],
  snapshot: { version: 1, version_id: 'v1', content_digest: 'c'.repeat(64), tool: 'csv_quote_xml' },
  result: { item_count: 3, total: '18920.70' }, created_at: '2026-09-17T02:00:00Z', ...over,
})
function mount() {
  return render(<MemoryRouter><CapabilityPanel agentId="a1" csrfToken="x" onUnauthorized={noop} /></MemoryRouter>)
}
function upload(name = '清单.csv') {
  const input = document.querySelector('.cv-attach input') as HTMLInputElement
  fireEvent.change(input, { target: { files: [new File(['编号,名称'], name, { type: 'text/csv' })] } })
}
beforeEach(() => { api.mockReset() })
afterEach(cleanup)

it('renders nothing for a role with no bound capability, leaving chat unchanged', async () => {
  api.mockImplementation(async () => ({ bindings: [] }) as never)
  const { container } = mount()
  await waitFor(() => expect(api).toHaveBeenCalled())
  expect(container.querySelector('.cv-capability')).toBeNull()
})

it('converts an uploaded file with the bound version and offers the real result for download', async () => {
  const posts: string[] = []
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (url?.includes('/bindings/')) return { bindings: [binding] } as never
    if (opts?.method === 'POST') {
      posts.push(String((opts.body as FormData).get('operation_key')))
      expect((opts.body as FormData).get('pack_id')).toBe('p1')
      return task({ status: 'running', outputs: [] }) as never
    }
    return task() as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  upload()
  await screen.findByText('转换完成')
  expect(screen.getByText('所用能力版本 v1')).toBeTruthy()
  const link = screen.getByText('quote.xml').closest('a') as HTMLAnchorElement
  expect(link.getAttribute('href')).toBe('/api/v4/capability-packs/artifacts/o1/download')
  expect(screen.getByText(/total：18920.70/)).toBeTruthy()
  expect(posts).toHaveLength(1)  // one submission, one stable operation key
})

it('a failed conversion keeps the candidate output but labels it unverified, with the reason', async () => {
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url?.includes('/bindings/')) return { bindings: [binding] } as never
    const failed = task({
      status: 'failed', validation_status: 'failed', error_code: 'validation_failed',
      error: '输出 quote.xml 与期望结构不一致', result: null,
      outputs: [{ id: 'o9', name: 'quote.xml', size: 12, validation_status: 'failed' }],
    })
    return (opts?.method === 'POST' ? task({ status: 'running', outputs: [] }) : failed) as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  upload()
  await screen.findByText('未通过校验')
  expect(screen.getByText(/输出 quote.xml 与期望结构不一致/)).toBeTruthy()
  expect(screen.getByText('未通过验证')).toBeTruthy()  // the file is still downloadable, never called verified
  expect(screen.getByText('quote.xml').closest('a')?.getAttribute('href'))
    .toBe('/api/v4/capability-packs/artifacts/o9/download')
})

it('an unavailable environment blocks the upload and points at settings, without hiding the capability', async () => {
  api.mockImplementation(async () => ({
    bindings: [{ ...binding, environment: { status: 'unavailable' as const, missing: ['lxml'] } }],
  }) as never)
  mount()
  await screen.findByText('CSV 清单 → XML')
  expect(screen.getByText(/环境缺依赖：lxml/)).toBeTruthy()
  expect(document.querySelector('.cv-attach input')).toBeNull()
})

it('a newer published version is offered, never applied silently', async () => {
  api.mockImplementation(async () => ({
    bindings: [{ ...binding, latest_version: 2, upgrade_available: true }],
  }) as never)
  mount()
  await screen.findByText('可升级到 v2')
  expect(screen.getByText('v1', { exact: false })).toBeTruthy()  // the binding still points at v1
})

it('a stale invocation result never lands on a different role (generation guard)', async () => {
  const gate: { release: ((value: unknown) => void) | null } = { release: null }
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url?.includes('/bindings/a1')) return { bindings: [binding] } as never
    if (url?.includes('/bindings/a2')) return { bindings: [] } as never
    if (opts?.method === 'POST') return task({ status: 'running', outputs: [] }) as never
    await new Promise(resolve => { gate.release = resolve })  // the poll for role A hangs
    return task() as never
  })
  const view = render(<MemoryRouter><CapabilityPanel agentId="a1" csrfToken="x" onUnauthorized={noop} /></MemoryRouter>)
  await screen.findByText('CSV 清单 → XML')
  upload()
  await screen.findByText('处理中…', { selector: '.cv-result-head strong' })
  view.rerender(<MemoryRouter><CapabilityPanel agentId="a2" csrfToken="x" onUnauthorized={noop} /></MemoryRouter>)
  await waitFor(() => expect(screen.queryByText('CSV 清单 → XML')).toBeNull())
  gate.release?.(null)
  await new Promise(resolve => setTimeout(resolve, 30))
  expect(screen.queryByText('转换完成')).toBeNull()
})
