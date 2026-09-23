// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import ReposPage from './ReposPage'
import { maintenanceApi } from './api'
import type { RepoView } from './types'
import { WorkspaceApiError } from '../workspace/api'

vi.mock('./api', () => ({
  maintenanceApi: {
    repos: vi.fn(),
    repo: vi.fn(),
    registerRepo: vi.fn(),
    probeRepo: vi.fn(),
    adoptChecks: vi.fn(),
  },
}))
const api = vi.mocked(maintenanceApi)
const noop = vi.fn()
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

const readyRepo: RepoView = {
  project_id: 'proj-1',
  name: '订单服务',
  repository: 'org/orders',
  workspace: '/data/orders',
  base_branch: 'main',
  state: 'ready',
  state_label: '可开始维护',
  probe: {
    at: '2026-09-22T10:00:00Z',
    head_sha: 'a'.repeat(40),
    branch: 'main',
    remote: 'origin',
    access: { ok: true, message: '' },
    stack: [{ name: 'Python/FastAPI', evidence: '发现 requirements.txt 与 main.py' }],
    suggested_checks: [{ name: 'pytest', argv: ['pytest', '-q'], evidence: '发现 tests/ 目录' }],
    findings: [
      { id: 'f1', label: '构建脚本', status: 'found', message: '发现 Makefile' },
      { id: 'f2', label: '单元测试可运行', status: 'verified', message: '试跑通过' },
    ],
  },
  needs: [],
  checks_configured: [],
  memory: { entries: 3, confirmed: 1 },
  credential_ref: null,
}

function renderList() {
  return render(
    <MemoryRouter initialEntries={['/maintenance/repos']}>
      <Routes>
        <Route path="maintenance/repos" element={<ReposPage csrfToken="csrf" onUnauthorized={noop} />} />
        <Route path="maintenance/repos/:projectId" element={<ReposPage csrfToken="csrf" onUnauthorized={noop} />} />
      </Routes>
    </MemoryRouter>,
  )
}

function renderDetail(projectId: string) {
  return render(
    <MemoryRouter initialEntries={[`/maintenance/repos/${projectId}`]}>
      <Routes>
        <Route path="maintenance/repos/:projectId" element={<ReposPage csrfToken="csrf" onUnauthorized={noop} />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('维护代码库 · 列表', () => {
  it('加载中显示占位，加载后显示登记表单与仓库行', async () => {
    api.repos.mockResolvedValue({ repos: [readyRepo] })
    renderList()
    await waitFor(() => expect(screen.getByText('订单服务')).toBeTruthy())
    expect(screen.getByText('可开始维护')).toBeTruthy()
    expect(screen.getByPlaceholderText('git@github.com:org/repo.git 或 执行主机上的本地目录')).toBeTruthy()
  })

  it('没有仓库时显示空状态', async () => {
    api.repos.mockResolvedValue({ repos: [] })
    renderList()
    await waitFor(() => expect(screen.getByText('还没有登记代码库')).toBeTruthy())
  })

  it('403 显示无权限提示', async () => {
    api.repos.mockRejectedValue(new WorkspaceApiError(403, '无权限'))
    renderList()
    await waitFor(() => expect(screen.getByText(/没有权限/)).toBeTruthy())
  })

  it('网络错误显示真实错误信息', async () => {
    api.repos.mockRejectedValue(new WorkspaceApiError(0, '网络请求失败'))
    renderList()
    await waitFor(() => expect(screen.getByText(/网络请求失败/)).toBeTruthy())
  })

  it('提交登记表单调用 registerRepo 并把新仓库加入列表', async () => {
    api.repos.mockResolvedValue({ repos: [] })
    api.registerRepo.mockResolvedValue({ ...readyRepo, project_id: 'proj-2', name: '新项目', state: 'pending', state_label: '待分析' })
    renderList()
    await waitFor(() => expect(screen.getByPlaceholderText('git@github.com:org/repo.git 或 执行主机上的本地目录')).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText('git@github.com:org/repo.git 或 执行主机上的本地目录'), { target: { value: 'git@github.com:org/new.git' } })
    fireEvent.change(screen.getByPlaceholderText('用于在列表中识别这个代码库'), { target: { value: '新项目' } })
    fireEvent.click(screen.getByRole('button', { name: '登记代码库' }))

    await waitFor(() => expect(api.registerRepo).toHaveBeenCalledWith(
      { source: 'git@github.com:org/new.git', name: '新项目', branch: undefined, credential_ref: undefined, synthetic: false },
      { csrfToken: 'csrf', onUnauthorized: noop },
    ))
    await waitFor(() => expect(screen.getByText('新项目')).toBeTruthy())
  })
})

describe('维护代码库 · 详情', () => {
  it('区分已发现与已验证的发现项', async () => {
    api.repo.mockResolvedValue({ ...readyRepo, requirements: [], tasks: [] })
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByTestId('finding-list')).toBeTruthy())
    expect(screen.getByTestId('finding-found').textContent).toContain('已发现')
    expect(screen.getByTestId('finding-verified').textContent).toContain('已验证')
  })

  it('建议检查非空时显示采纳按钮，点击调用 adoptChecks', async () => {
    api.repo.mockResolvedValue(readyRepo)
    api.adoptChecks.mockResolvedValue({ ...readyRepo, checks_configured: ['pytest'] })
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByText('采纳建议检查')).toBeTruthy())
    fireEvent.click(screen.getByText('采纳建议检查'))
    await waitFor(() => expect(api.adoptChecks).toHaveBeenCalledWith('proj-1', ['pytest'], { csrfToken: 'csrf', onUnauthorized: noop }))
  })

  it('Docker 检查显示容器内命令而不是内部执行标记', async () => {
    api.repo.mockResolvedValue({ ...readyRepo, probe: { ...readyRepo.probe!, suggested_checks: [
      { name: 'pytest', argv: ['@dockerfile', 'python3', '-m', 'pytest', '-q'],
        evidence: 'Dockerfile', available: true },
    ] } })
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByText('Docker 容器内：python3 -m pytest -q')).toBeTruthy())
    expect(screen.queryByText(/@dockerfile/)).toBeNull()
  })

  it('建议检查为空时不显示采纳按钮', async () => {
    api.repo.mockResolvedValue({ ...readyRepo, probe: { ...readyRepo.probe!, suggested_checks: [] } })
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByText('订单服务')).toBeTruthy())
    expect(screen.queryByText('采纳建议检查')).toBeNull()
  })

  it('显示「可开始维护不代表构建部署与业务检查通过」的说明', async () => {
    api.repo.mockResolvedValue(readyRepo)
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByText(/不代表构建、部署与业务检查通过/)).toBeTruthy())
  })

  it('不把项目记忆误报成代码索引状态，并提供真实入口', async () => {
    api.repo.mockResolvedValue(readyRepo)
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByRole('button', { name: '打开代码索引' })).toBeTruthy())
    expect(screen.getByText('已记录的维护知识')).toBeTruthy()
    expect(screen.queryByText('执行时现场检索，未预建索引')).toBeNull()
  })

  it('管理员从维护仓库打开索引，查看独立层状态并真正触发构建和搜索', async () => {
    api.repo.mockResolvedValue(readyRepo)
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const data = path.endsWith('/agent')
        ? { id: 'agent', project_id: 'proj-1', revision: 1, name: '订单服务', mission: '', architecture_summary: '', constraints: [] }
        : path.endsWith('/code-index') && init?.method === 'POST'
          ? { indexed: true, commit_sha: 'a'.repeat(40), current_sha: 'a'.repeat(40), indexed_at: '2026-09-23T12:00:00Z', shared_layer: { indexed: false, reason: '共享索引工具不可用' } }
          : path.endsWith('/code-index')
            ? { indexed: false, shared_layer: { indexed: false, reason: '尚未构建' } }
            : path.includes('/code-search?')
              ? { results: [{ node_id: 'f:app.py', path: 'app.py', name: 'render', kind: 'function', line: 1, end_line: 2, score: 1, snippet: 'def render', resolution: 'syntax' }], commit_sha: 'a'.repeat(40), current_sha: 'a'.repeat(40), stale: false, warnings: [] }
              : {}
      return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetcher)
    renderDetail('proj-1')
    fireEvent.click(await screen.findByRole('button', { name: '打开代码索引' }))
    expect(await screen.findByText('尚未建立代码索引。')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '构建 / 刷新索引' }))
    expect(await screen.findByText('索引 SHA：' + 'a'.repeat(40))).toBeTruthy()
    expect(screen.getByText('共享索引工具不可用')).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText('文件名、符号或中文文档'), { target: { value: 'render' } })
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))
    expect(await screen.findByText('app.py:1–2')).toBeTruthy()
    expect(fetcher).toHaveBeenCalledWith('/api/v2/projects/proj-1/code-index', expect.objectContaining({ method: 'POST' }))
    expect(fetcher).toHaveBeenCalledWith('/api/v2/projects/proj-1/code-search?q=render', expect.objectContaining({ method: 'GET' }))
  })

  it('点击重新分析调用 probeRepo', async () => {
    api.repo.mockResolvedValue(readyRepo)
    api.probeRepo.mockResolvedValue(readyRepo)
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByText('重新分析')).toBeTruthy())
    fireEvent.click(screen.getByText('重新分析'))
    await waitFor(() => expect(api.probeRepo).toHaveBeenCalledWith('proj-1', { csrfToken: 'csrf', onUnauthorized: noop }))
  })
})

describe('维护代码库 · 合成标记', () => {
  it('登记时显式勾选合成，请求携带 synthetic，列表显示「合成」', async () => {
    api.repos.mockResolvedValue({ repos: [] })
    api.registerRepo.mockResolvedValue({ ...readyRepo, project_id: 'proj-3', name: '演示库', synthetic: true })
    renderList()
    await waitFor(() => expect(screen.getByPlaceholderText('用于在列表中识别这个代码库')).toBeTruthy())
    fireEvent.change(screen.getByPlaceholderText('git@github.com:org/repo.git 或 执行主机上的本地目录'), { target: { value: '/srv/demo' } })
    fireEvent.change(screen.getByPlaceholderText('用于在列表中识别这个代码库'), { target: { value: '演示库' } })
    fireEvent.click(screen.getByLabelText(/这是合成\/演示仓库/))
    fireEvent.click(screen.getByRole('button', { name: '登记代码库' }))
    await waitFor(() => expect(api.registerRepo).toHaveBeenCalledWith(
      expect.objectContaining({ synthetic: true }), expect.anything()))
    await waitFor(() => expect(screen.getByText('合成')).toBeTruthy())
  })

  it('未声明的仓库不显示「合成」', async () => {
    api.repos.mockResolvedValue({ repos: [readyRepo] })
    renderList()
    await waitFor(() => expect(screen.getByText('订单服务')).toBeTruthy())
    expect(screen.queryByText('合成')).toBeNull()
  })
})

describe('接入失败的仓库', () => {
  const failed: RepoView = {
    ...readyRepo, project_id: 'proj-private', name: 'group-risk', repository: 'auntunt/group-risk-data-system',
    state: 'failed', state_label: '接入失败',
    probe: { ...readyRepo.probe!, head_sha: null, access: {
      ok: false, reason: 'auth', message: '执行主机没有访问该仓库的凭据',
      next_step: '管理员在服务端配置 FACTORY_GITHUB_TOKEN 后点“重新接入”',
      detail: "fatal: could not read Username for 'https://github.com': terminal prompts disabled", credential: null } },
  }

  it('列表直接显示原因、下一步，并能重新接入', async () => {
    api.repos.mockResolvedValue({ repos: [failed] })
    api.probeRepo.mockResolvedValue({ ...failed, state: 'analyzing', state_label: '分析中', probe: null })
    renderList()
    expect(await screen.findByText('执行主机没有访问该仓库的凭据')).toBeTruthy()
    expect(screen.getByText(/下一步：管理员在服务端配置 FACTORY_GITHUB_TOKEN/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重新接入' }))
    await waitFor(() => expect(api.probeRepo).toHaveBeenCalledWith('proj-private', expect.objectContaining({ csrfToken: 'csrf' })))
    expect(await screen.findByText('分析中')).toBeTruthy()
  })
})

describe('没有检查命令的仓库详情', () => {
  const bare: RepoView = { ...readyRepo, state: 'needs_input', state_label: '待补充', checks_configured: [],
    needs: ['确认检查命令'], probe: { ...readyRepo.probe!, suggested_checks: [] } }

  function renderDetail(user?: { id: number; username: string; role: 'admin' | 'member' }) {
    return render(
      <MemoryRouter initialEntries={['/maintenance/repos/proj-1']}>
        <Routes><Route path="maintenance/repos/:projectId" element={<ReposPage csrfToken="csrf" onUnauthorized={noop} user={user as never} />} /></Routes>
      </MemoryRouter>)
  }

  it('如实说明缺少配置；管理员得到真实配置入口，不出现采纳按钮', async () => {
    api.repo.mockResolvedValue(bare)
    renderDetail({ id: 1, username: 'a', role: 'admin' })
    expect(await screen.findByText('尚未配置，也没有可建议的命令')).toBeTruthy()
    expect(screen.getByRole('link', { name: '在项目设置里配置检查命令' }).getAttribute('href')).toBe('/projects/proj-1#project-checks')
    expect(screen.queryByRole('button', { name: '采纳建议检查' })).toBeNull()
  })

  it('成员只看到需要管理员配置的说明，没有死链接', async () => {
    api.repo.mockResolvedValue(bare)
    renderDetail({ id: 2, username: 'm', role: 'member' })
    expect(await screen.findByText(/请管理员在 webuddy 的项目设置中/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: '在项目设置里配置检查命令' })).toBeNull()
  })
})
