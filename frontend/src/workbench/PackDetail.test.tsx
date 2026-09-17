// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import PackDetail from './PackDetail'
import PacksPage from './PacksPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()

const manifest = {
  name: 'CSV 清单 → XML', purpose: '把工程量清单转换为 XML',
  tool: { name: 'csv_quote_xml', entrypoint: 'tool/main.py', runtime: 'python3', timeout_seconds: 30 },
  dependency_lock: { python: '3', packages: [] },
  support_matrix: [{ format: 'UTF-8 CSV', status: 'supported', evidence: '逐字节比对' },
                   { format: 'xlsx', status: 'unsupported', evidence: '本版本不解析' }],
  evaluation_policy: { test_set: 'fixtures/tests.json', required: true },
}
const files = [{ path: 'tool/main.py', sha256: 'a'.repeat(64), size: 900, role: 'program' as const },
               { path: 'fixtures/sample.csv', sha256: 'b'.repeat(64), size: 100, role: 'fixture' as const, material_scope: 'synthetic' }]

function detail(overrides: Record<string, unknown> = {}) {
  return {
    id: 'p1', name: 'CSV 清单 → XML', purpose: '把工程量清单转换为 XML', owner_id: '1',
    updated_at: '2026-09-17T02:00:00Z', can_maintain: true, versions: [], evaluations: [],
    bindings: [], environments: {},
    draft: { revision: 3, lifecycle: 'draft', blocked_reason: null, manifest, files,
             content_digest: 'c'.repeat(64), excluded: [{ path: 'input/客户清单.csv', reason: '客户原始资料默认不入包' }],
             source_task_id: 'run-1', evaluations: [] },
    ...overrides,
  }
}
function mount(path = '/ability-center/packs/p1') {
  return render(<MemoryRouter initialEntries={[path]}>
    <Routes><Route path="/ability-center/packs/:packId" element={<PackDetail csrfToken="x" onUnauthorized={noop} />} /></Routes>
  </MemoryRouter>)
}
beforeEach(() => { api.mockReset() })  // braces: returning the mock makes vitest await it and surface the rejection
afterEach(cleanup)

it('shows a load failure instead of an empty catalogue that looks like "no capabilities"', async () => {
  api.mockImplementation(async () => { throw new Error('服务暂时不可用') })
  render(<MemoryRouter><PacksPage onUnauthorized={noop} csrfToken="x" /></MemoryRouter>)
  await screen.findByText('职能包目录加载失败')
  expect(screen.queryByText('还没有职能包')).toBeNull()
})

it('refuses to offer publish until evidence exists for the CURRENT candidate content', async () => {
  api.mockImplementation(async () => detail({
    // evidence from an older candidate: present in history, not credited to this content
    evaluations: [{ id: 'e-old', content_digest: 'd'.repeat(64), created_at: '2026-09-17T01:00:00Z',
                    passed: true, summary: '2/2 个用例通过', cases: [] }],
  }) as never)
  mount('/ability-center/packs/p1?tab=validation')
  await screen.findByText('运行验证')
  expect((screen.getByText('发布此版本') as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText(/当前候选内容尚未运行验证/)).toBeTruthy()
  expect(screen.getByText(/与当前内容不同，不能用于发布/)).toBeTruthy()
})

it('enables publish only with passing evidence bound to the current digest, and publishes that revision', async () => {
  const evidence = { id: 'e1', content_digest: 'c'.repeat(64), created_at: '2026-09-17T02:00:00Z',
                     passed: true, summary: '2/2 个用例通过', test_set: 'fixtures/tests.json',
                     environment: { python: '3.12.7', sandbox: 'seatbelt' },
                     cases: [{ id: '标准清单转换', passed: true }] }
  const posts: Array<Record<string, unknown>> = []
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (opts?.method === 'POST') { posts.push({ url, body: opts.body }); return { version: 1 } as never }
    return detail({ evaluations: [evidence], draft: { ...detail().draft, evaluations: [evidence] } }) as never
  })
  mount('/ability-center/packs/p1?tab=validation')
  const publish = await screen.findByText('发布此版本')
  expect((publish as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(publish)
  await screen.findByText(/已发布 v1/)
  expect(posts[0].url).toBe('/api/v4/capability-packs/p1/versions')
  expect(posts[0].body).toMatchObject({ expected_revision: 3, evaluation_id: 'e1' })
  expect(screen.getByText(/不等于生产部署/)).toBeTruthy()
})

it('an unavailable environment never turns a published version back into "not published"', async () => {
  const version = { id: 'v1', pack_id: 'p1', version: 1, content_digest: 'c'.repeat(64),
                    created_at: '2026-09-17T02:00:00Z', manifest, files, evaluation_id: 'e1',
                    support_matrix: manifest.support_matrix, lifecycle: 'published' }
  api.mockImplementation(async () => detail({
    versions: [version], draft: { ...detail().draft, lifecycle: 'published' },
    environments: { v1: { status: 'unavailable', missing: ['lxml'] } },
  }) as never)
  mount()
  await screen.findByText('已发布')
  expect(screen.getByText('环境缺依赖')).toBeTruthy()
  expect(screen.getByText(/本机环境缺少依赖：lxml/)).toBeTruthy()
  expect(screen.queryByText('未发布')).toBeNull()
})

it('runs a real validation job, reports its summary and reloads the pack', async () => {
  let job = 'running'
  const calls: string[] = []
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    calls.push(`${opts?.method || 'GET'} ${url}`)
    if (opts?.method === 'POST') return { job_id: 'j1', status: 'pending' } as never
    if (url?.includes('/jobs/j1')) {
      const status = job; job = 'completed'
      return { status, result: status === 'completed' ? { passed: false, summary: '1/2 个用例通过；验证未通过，不可发布' } : undefined } as never
    }
    return detail() as never
  })
  mount('/ability-center/packs/p1?tab=validation')
  fireEvent.click(await screen.findByText('运行验证'))
  await waitFor(() => expect(screen.getByText('1/2 个用例通过；验证未通过，不可发布')).toBeTruthy(), { timeout: 5000 })
  expect(calls).toContain('POST /api/v4/capability-packs/p1/evaluations')
  expect(calls.filter(c => c === 'GET /api/v4/capability-packs/p1').length).toBeGreaterThan(1)
})

it('lists pack content with excluded client material and its reason', async () => {
  api.mockImplementation(async () => detail() as never)
  mount('/ability-center/packs/p1?tab=content')
  await screen.findByText('能力内容清单')
  expect(screen.getByText('未纳入的文件')).toBeTruthy()
  expect(screen.getByText(/客户原始资料默认不入包/)).toBeTruthy()
  expect(screen.getByText('测试样本 · 合成')).toBeTruthy()
})

it('shows the validated scope honestly, including formats that are not supported', async () => {
  api.mockImplementation(async () => detail() as never)
  mount()
  await screen.findByText('适用范围')
  expect(screen.getByText('已验证')).toBeTruthy()
  expect(screen.getByText('暂不支持')).toBeTruthy()
})
