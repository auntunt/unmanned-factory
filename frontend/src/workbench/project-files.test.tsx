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

it('creates a sample project inside the current agent and keeps it selected for the task',async()=>{
  const {AgentChat}=await import('./AgentsPage')
  const agent={id:'a',name:'格式转换',active_version:1}
  vi.mocked(request).mockImplementation(async(path,options)=>{
    if(path==='/api/v4/agents/a') return agent
    if(path==='/api/v4/agents') return {agents:[agent]}
    if(String(path).endsWith('/conversations')) return {conversations:[]}
    if(path==='/api/v2/projects') return {projects:[]}
    if(path==='/api/v2/projects/import-files') return {project:{id:'p',name:'转换样本'},import_summary:{filename:'样本',file_count:1,manifests:[],warnings:[],baseline_status:'not_run'}}
    throw new Error(`Unexpected ${path} ${options?.method}`)
  })
  render(<MemoryRouter><AgentChat agent={agent} props={{csrfToken:'csrf',onUnauthorized:()=>{}}}/></MemoryRouter>)
  await screen.findByText('今天想让格式转换完成什么？')
  fireEvent.click(screen.getByRole('button',{name:'上传资料并建立项目'}))
  await screen.findByText('格式转换 · v1')
  expect((screen.getByLabelText(/选择智能体帮助/) as HTMLSelectElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText(/项目与样例文件/),{target:{files:[new File(['sample'],'sample.xyz')]}})
  fireEvent.click(screen.getByRole('button',{name:'导入项目'}))
  await waitFor(()=>expect((screen.getByLabelText('工作项目') as HTMLSelectElement).value).toBe('p'))
  expect(screen.getByRole('status').textContent).toContain('助手可直接读取这些文件')
  expect(vi.mocked(request).mock.calls.some(([path,options])=>String(path).endsWith('/messages') && options?.method==='POST')).toBe(false)
})
