// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import CapabilityPanel from './CapabilityPanel'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()

const toolContract = {
  permissions: { network: false, max_input_bytes: 4194304, max_output_bytes: 8388608 },
  timeout_seconds: 30,
  support_matrix: [
    { format: 'UTF-8 CSV，表头含 编号/名称/单位/数量/单价', status: 'supported' as const, evidence: 'fixture' },
    { format: 'GBK 等非 UTF-8 编码', status: 'unsupported' as const, evidence: 'not_utf8' },
  ],
  purpose: '把 UTF-8 的工程量清单 CSV 转换为结构化 XML',
  input_schema: null,
  output_schema: null,
}

const binding = {
  id: 'b1', agent_id: 'a1', pack_id: 'p1', pack_name: 'CSV 清单 → XML', version_id: 'v1', version: 1,
  revision: 1, latest_version: 1, upgrade_available: false, content_digest: 'c'.repeat(64),
  environment: { status: 'ready' as const },
  tool_contract: toolContract,
}

const bindingNoContract = {
  ...binding, id: 'b2', pack_id: 'p2', pack_name: '未声明包', tool_contract: null,
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
    if (url?.includes('/invocations') && !opts?.method && !url?.includes('/invocations/')) return { tasks: [] } as never
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
  await screen.findByText('处理完成')
  expect(screen.getByText('所用能力版本 v1')).toBeTruthy()
  const link = screen.getByText('quote.xml').closest('a') as HTMLAnchorElement
  expect(link.getAttribute('href')).toBe('/api/v4/capability-packs/artifacts/o1/download')
  expect(screen.getByText(/total：18920.70/)).toBeTruthy()
  expect(posts).toHaveLength(1)  // one submission, one stable operation key
})

it('a failed conversion keeps the candidate output but labels it unverified, with the reason', async () => {
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url?.includes('/bindings/')) return { bindings: [binding] } as never
    if (url?.includes('/invocations') && !opts?.method && !url?.includes('/invocations/')) return { tasks: [] } as never
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
  await screen.findByText('执行失败')
  expect(screen.getByText(/输出 quote.xml 与期望结构不一致/)).toBeTruthy()
  expect(screen.getByText('未通过验证')).toBeTruthy()
  expect(screen.getByText('quote.xml').closest('a')?.getAttribute('href'))
    .toBe('/api/v4/capability-packs/artifacts/o9/download')
})

it('a cancelled invocation renders distinctly from a failure, with its own label', async () => {
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url?.includes('/bindings/')) return { bindings: [binding] } as never
    if (url?.includes('/invocations') && !opts?.method && !url?.includes('/invocations/')) return { tasks: [] } as never
    const cancelled = task({
      status: 'cancelled', validation_status: 'not_applicable', error_code: 'cancelled',
      error: '调用已取消', result: null, outputs: [],
    })
    return (opts?.method === 'POST' ? task({ status: 'running', outputs: [] }) : cancelled) as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  upload()
  await screen.findByText('已取消')
  expect(screen.getByText(/该调用已被取消/)).toBeTruthy()
  expect(document.querySelector('.cv-result.is-cancelled')).toBeTruthy()
})

it('an unavailable environment blocks the upload and points at settings, without hiding the capability', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return {
      bindings: [{ ...binding, environment: { status: 'unavailable' as const, missing: ['lxml'] } }],
    } as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  expect(screen.getByText(/环境缺依赖：lxml/)).toBeTruthy()
  expect(document.querySelector('.cv-attach input')).toBeNull()
})

it('a newer published version is offered, never applied silently', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return {
      bindings: [{ ...binding, latest_version: 2, upgrade_available: true }],
    } as never
  })
  mount()
  await screen.findByText('可升级到 v2')
  expect(screen.getByText('v1', { exact: false })).toBeTruthy()
})

it('a stale invocation result never lands on a different role (generation guard)', async () => {
  const gate: { release: ((value: unknown) => void) | null } = { release: null }
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url?.includes('/bindings/a1')) return { bindings: [binding] } as never
    if (url?.includes('/bindings/a2')) return { bindings: [] } as never
    if (url?.includes('/invocations') && !opts?.method && !url?.includes('/invocations/')) return { tasks: [] } as never
    if (opts?.method === 'POST') return task({ status: 'running', outputs: [] }) as never
    await new Promise(resolve => { gate.release = resolve })  // the poll for role A hangs
    return task() as never
  })
  const view = render(<MemoryRouter><CapabilityPanel agentId="a1" csrfToken="x" onUnauthorized={noop} /></MemoryRouter>)
  await screen.findByText('CSV 清单 → XML')
  upload()
  await screen.findByText('处理中', { selector: '.cv-result-head strong' })
  view.rerender(<MemoryRouter><CapabilityPanel agentId="a2" csrfToken="x" onUnauthorized={noop} /></MemoryRouter>)
  await waitFor(() => expect(screen.queryByText('CSV 清单 → XML')).toBeNull())
  gate.release?.(null)
  await new Promise(resolve => setTimeout(resolve, 30))
  expect(screen.queryByText('处理完成')).toBeNull()
})

// ---- capability declaration rendering ----

it('shows input constraints from tool contract: max size and timeout', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return { bindings: [binding] } as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  expect(screen.getByText(/上限 4.0 MB/)).toBeTruthy()
  expect(screen.getByText(/超时 30s/)).toBeTruthy()
})

it('shows purpose from tool contract', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return { bindings: [binding] } as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  expect(screen.getByText(/把 UTF-8 的工程量清单 CSV 转换为结构化 XML/)).toBeTruthy()
})

it('shows support matrix when toggled', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return { bindings: [binding] } as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  const toggle = screen.getByText('支持范围')
  fireEvent.click(toggle)
  expect(screen.getByText(/UTF-8 CSV/)).toBeTruthy()
  expect(screen.getByText('已验证')).toBeTruthy()
  expect(screen.getByText('暂不支持')).toBeTruthy()
})

it('shows "undeclared" when tool contract is null', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return { bindings: [bindingNoContract] } as never
  })
  mount()
  await screen.findByText('未声明包')
  expect(screen.getByText('能力声明未加载')).toBeTruthy()
})

it('restores the last task result from invocation history on mount', async () => {
  const previous = task({ status: 'succeeded' })
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/bindings/')) return { bindings: [binding] } as never
    if (url?.includes('/invocations') && !url?.includes('/invocations/')) return { tasks: [previous] } as never
    return previous as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  await screen.findByText('处理完成')
  expect(screen.getByText('quote.xml')).toBeTruthy()
})

it('client-side rejects a file that exceeds the declared max_input_bytes', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/invocations')) return { tasks: [] } as never
    return { bindings: [binding] } as never
  })
  mount()
  await screen.findByText('CSV 清单 → XML')
  const input = document.querySelector('.cv-attach input') as HTMLInputElement
  // Create a file larger than 4 MB
  const bigContent = new Uint8Array(4194305)
  fireEvent.change(input, { target: { files: [new File([bigContent], 'big.csv', { type: 'text/csv' })] } })
  await screen.findByText(/超过该能力的上限/)
})

it('does not restore another conversation result when used inside chat', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url?.includes('/bindings/')) return { bindings: [binding] } as never
    return { tasks: [task()] } as never
  })
  render(<MemoryRouter><CapabilityPanel agentId="a1" csrfToken="x" onUnauthorized={noop} restoreRecent={false} /></MemoryRouter>)
  await screen.findByText('CSV 清单 → XML')
  expect(api.mock.calls.some(([url]) => url === '/api/v4/capability-packs/invocations')).toBe(false)
  expect(screen.queryByText('quote.xml')).toBeNull()
})
