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
afterEach(() => { cleanup(); vi.clearAllMocks() })

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
  memory: { entries: 3, confirmed: 1, code_index: 'on_demand' },
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
      { source: 'git@github.com:org/new.git', name: '新项目', branch: undefined, credential_ref: undefined },
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

  it('记忆信息 on_demand 显示为「执行时现场检索，未预建索引」', async () => {
    api.repo.mockResolvedValue(readyRepo)
    renderDetail('proj-1')
    await waitFor(() => expect(screen.getByText('执行时现场检索，未预建索引')).toBeTruthy())
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
