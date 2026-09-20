// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {ProjectForm} from './ProjectsPage'
import {request} from '../workspace/api'
vi.mock('../workspace/api',()=>({request:vi.fn(),WorkspaceApiError:class extends Error{}}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

it.each([false,true])('uploads samples or extracts a single ZIP with the selected agent (zip=%s)',async zip=>{
  const created=vi.fn()
  vi.mocked(request).mockImplementation(async(path)=>path==='/api/v4/agents' ? {agents:[{id:'a',name:'格式转换',active_version:1}]} : {project:{id:'p'},import_summary:{filename:'材料',file_count:2,manifests:[],warnings:[],baseline_status:'not_run'}})
  render(<MemoryRouter><ProjectForm csrfToken="csrf" onUnauthorized={()=>{}} onCreated={created} onCancel={()=>{}}/></MemoryRouter>)
  await screen.findByText('格式转换 · v1')
  fireEvent.change(screen.getByLabelText(/选择智能体帮助/),{target:{value:'a'}})
  fireEvent.change(screen.getByLabelText('工程来源'),{target:{value:'zip'}})
  const files=zip ? [new File(['archive'],'format.zip')] : [new File([new Uint8Array([0,255])],'sample.custom'),new File(['说明'],'readme.txt')]
  fireEvent.change(screen.getByLabelText(/项目与样例文件/),{target:{files}})
  expect(screen.getByRole('status').textContent).toContain(zip ? 'ZIP 将安全解包' : '原始文件直接保存')
  fireEvent.click(screen.getByRole('button',{name:'导入项目'}))
  await waitFor(()=>expect(created).toHaveBeenCalled())
  const call=vi.mocked(request).mock.calls.find(([url])=>String(url).includes('/import-'))!
  expect(call[0]).toBe(`/api/v2/projects/import-${zip?'zip':'files'}`)
  const body=call[1]!.body as FormData
  expect(body.get('agent_id')).toBe('a')
  expect(body.getAll(zip?'file':'files')).toHaveLength(files.length)
})

it('creates a sample project within the current agent context from the management page', async () => {
  const created = vi.fn()
  vi.mocked(request).mockImplementation(async (path) =>
    path === '/api/v4/agents' ? { agents: [{ id: 'agent-x', name: '测试职能体', active_version: 2 }] }
    : { project: { id: 'proj-1', name: '样例项目' }, import_summary: { filename: '材料', file_count: 1, manifests: [], warnings: [], baseline_status: 'not_run' } }
  )
  // ProjectForm with agentId pre-set simulates the management page embedding:
  // the agent_id is fixed by the caller, not re-selected in a dropdown.
  render(<MemoryRouter><ProjectForm csrfToken="csrf" onUnauthorized={() => {}} onCreated={created} onCancel={() => {}} agentId="agent-x" uploadFirst /></MemoryRouter>)
  // When agentId is provided, the form should still load agents for display
  await waitFor(() => expect(vi.mocked(request)).toHaveBeenCalled())
  fireEvent.change(screen.getByLabelText('工程来源'), { target: { value: 'zip' } })
  const file = new File(['archive'], 'sample.zip')
  fireEvent.change(screen.getByLabelText(/项目与样例文件/), { target: { files: [file] } })
  fireEvent.click(screen.getByRole('button', { name: '导入项目' }))
  await waitFor(() => expect(created).toHaveBeenCalled())
  const call = vi.mocked(request).mock.calls.find(([url]) => String(url).includes('/import-'))!
  expect(call[0]).toBe('/api/v2/projects/import-zip')
  const body = call[1]!.body as FormData
  expect(body.get('agent_id')).toBe('agent-x')
})
