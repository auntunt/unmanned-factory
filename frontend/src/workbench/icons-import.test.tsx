// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Icon, { CategoryBadge } from './Icon'
import Workbench from './Workbench'
import ModulesPage from './ModulesPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'admin', role: 'admin' as const } }
beforeEach(() => vi.mocked(request).mockImplementation(async path => path === '/api/v3/environment' ? { mode: 'preview', label: '演练' } : path === '/api/v4/modules' ? { modules: [{ id: 'm', name: '代码梳理', category: 'workflow', version: 1, instructions: '读取代码', description: '整理项目' }] } : { projects: [], items: [] }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('uses decorative fill SVG with viewBox and avatar fallback', () => {
 const { container } = render(<><Icon name="plus" /><CategoryBadge category="knowledge" /><CategoryBadge avatar="维" /><CategoryBadge avatar="" /></>)
 for (const svg of container.querySelectorAll('svg')) {
  expect(svg.getAttribute('viewBox')).toBe('0 0 256 256'); expect(svg.getAttribute('fill')).toBe('currentColor'); expect(svg.getAttribute('aria-hidden')).toBe('true')
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
