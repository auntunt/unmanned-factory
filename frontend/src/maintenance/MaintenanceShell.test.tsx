// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import MaintenanceShell from './MaintenanceShell'

afterEach(cleanup)

function renderShell(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="maintenance" element={<MaintenanceShell />}>
          <Route index element={<div>监控内容</div>} />
          <Route path="repos" element={<div>代码库内容</div>} />
          <Route path="intake" element={<div>接入内容</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('MaintenanceShell 子系统外框', () => {
  it('渲染三个主入口和面包屑，默认命中运维监控', () => {
    renderShell('/maintenance')
    expect(screen.getByText('webuddy')).toBeTruthy()
    expect(screen.getByText('运维维护子系统')).toBeTruthy()
    expect(screen.getByText('运维监控')).toBeTruthy()
    expect(screen.getByText('维护代码库')).toBeTruthy()
    expect(screen.getByText('需求接入')).toBeTruthy()
    expect(screen.getByText('监控内容')).toBeTruthy()
    expect(screen.getByText('运维监控').closest('a')?.getAttribute('aria-current')).toBe('page')
  })

  it('二级链接指向全部任务与技术详情', () => {
    renderShell('/maintenance')
    expect(screen.getByText('全部任务').closest('a')?.getAttribute('href')).toBe('/maintenance/tasks')
    expect(screen.getByText('技术详情').closest('a')?.getAttribute('href')).toBe('/maintenance/about')
  })

  it('子路由命中时对应页签高亮，渲染 Outlet 内容', () => {
    renderShell('/maintenance/repos')
    expect(screen.getByText('代码库内容')).toBeTruthy()
    expect(screen.getByText('维护代码库').closest('a')?.getAttribute('aria-current')).toBe('page')
    expect(screen.getByText('运维监控').closest('a')?.getAttribute('aria-current')).toBeNull()
  })
})
