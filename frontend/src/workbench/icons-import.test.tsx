// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Icon, { CategoryBadge } from './Icon'
import PackImport from './PackImport'
import Workbench from './Workbench'
import ModulesPage from './ModulesPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'admin', role: 'admin' as const } }
beforeEach(() => vi.mocked(request).mockImplementation(async path => path === '/api/v3/environment' ? { mode: 'preview', label: '演练' } : path === '/api/v4/modules' ? { modules: [{ id: 'm', name: '代码梳理', category: 'workflow', version: 1, instructions: '读取代码', description: '整理项目' }] } : { projects: [], items: [] }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('uses decorative SVG with one stroke specification and avatar fallback', () => {
 const { container } = render(<><Icon name="plus" /><CategoryBadge category="knowledge" /><CategoryBadge avatar="维" /><CategoryBadge avatar="" /></>)
 for (const svg of container.querySelectorAll('svg')) {
  expect(svg.getAttribute('viewBox')).toBe('0 0 24 24'); expect(svg.getAttribute('stroke')).toBe('currentColor'); expect(svg.getAttribute('stroke-width')).toBe('1.6'); expect(svg.getAttribute('aria-hidden')).toBe('true')
 }
 expect(container.querySelectorAll('.wb-category-badge svg')).toHaveLength(2)
 expect(screen.getByText('维').querySelector('svg')).toBeNull()
})
it('renders navigation, logout, menu and rehearsal icons as SVG without glyph text', async () => {
 render(<MemoryRouter><Workbench {...props} onLogout={props.onUnauthorized}><p>页面</p></Workbench></MemoryRouter>)
 await screen.findByText('本地演练')
 for (const name of ['退出登录', '打开导航']) expect(screen.getByRole('button', { name }).querySelector('svg')).toBeTruthy()
 for (const link of screen.getAllByRole('link').filter(el => el.closest('nav'))) { expect(link.getAttribute('aria-label')).toBeTruthy(); expect(link.querySelector('svg')).toBeTruthy() }
 expect(screen.getByRole('status').querySelector('svg')).toBeTruthy()
 expect(document.body.textContent).not.toMatch(/[↗☰◌▶▼←＋→]/)
})
it('groups import controls into switchable keyboard-accessible tabs, preserving input state', async () => {
 render(<MemoryRouter><PackImport {...props} onImported={vi.fn()} /></MemoryRouter>)
 const summary = screen.getByText('导入'); expect(summary.querySelector('svg[data-icon="triangle"]')).toBeTruthy()
 expect(summary.closest('details')?.open).toBe(false); fireEvent.click(summary)
 expect(screen.getByRole('tabpanel').getAttribute('id')).toBe('pack-panel-0')
 expect(screen.getByLabelText('选择 webuddy 职能包 ZIP').closest('[role=tabpanel]')?.hasAttribute('hidden')).toBe(false)
 const external = screen.getByRole('tab', { name: '外部 skill 包·适配人签' }); fireEvent.click(external)
 expect(screen.getByRole('tabpanel').getAttribute('id')).toBe('pack-panel-1')
 fireEvent.change(screen.getByLabelText('已挂载目录（相对项目工作区）'), { target: { value: 'skills/example' } })
 fireEvent.keyDown(external, { key: 'ArrowLeft' })
 expect(screen.getByRole('tab', { name: '职能包 v1/v2' }).getAttribute('aria-selected')).toBe('true')
 fireEvent.click(external)
 expect((screen.getByLabelText('已挂载目录（相对项目工作区）') as HTMLInputElement).value).toBe('skills/example')
 await screen.findByText('请选择项目')
})
it('renders module category and disclosure with SVG and gives content/edit matching link styles', async () => {
 render(<MemoryRouter><ModulesPage {...props} /></MemoryRouter>)
 const card = (await screen.findByText('代码梳理')).closest('article')!
 expect(card.querySelector('.wb-category-badge svg[data-icon=workflow]')).toBeTruthy()
 const content = within(card).getByText('查看内容'), edit = within(card).getByRole('button', { name: '编辑模块' })
 expect(content.classList.contains('wb-text-link')).toBe(true); expect(edit.classList.contains('wb-text-link')).toBe(true)
 expect(content.querySelector('svg[data-icon=triangle]')).toBeTruthy()
 fireEvent.click(content); expect(card.querySelector('details')?.open).toBe(true)
 expect(card.textContent).not.toMatch(/[↗☰◌▶▼←＋→]/)
})
it('opens the external ingestion tab from the maintenance upload guidance link', async () => {
 render(<MemoryRouter initialEntries={['/agents?import=external']}><PackImport {...props} onImported={vi.fn()} /></MemoryRouter>)
 expect(screen.getByText('导入').closest('details')?.open).toBe(true)
 expect(screen.getByRole('tab', {name:'外部 skill 包·适配人签'}).getAttribute('aria-selected')).toBe('true')
 expect(screen.getByRole('tabpanel').id).toBe('pack-panel-1')
})
