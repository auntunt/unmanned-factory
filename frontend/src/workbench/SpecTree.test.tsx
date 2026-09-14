// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { request } from '../workspace/api'
import SpecTree, { SpecNodeDetail, SpecDriftBadge, SpecSettings, SpecEvidenceLink, type SpecNode } from './SpecTree'
vi.mock('../workspace/api', async original => ({...await original<typeof import('../workspace/api')>(),request:vi.fn()}))
const api=vi.mocked(request)
const node: SpecNode={path:'.spec/app/spec.md',title:'订单计算',status:'active',depth:0,code_count:1,desc:'orders',errors:[],raw_source:'金额必须精确',expanded:'## 行为\n\n**精确计算**\n\n<script>alert(1)</script>\n\n[危险](javascript:alert(1))',code:[{entry:'app.py#calculate',path:'app.py',symbol:'calculate'}],related:[{entry:'README.md',path:'README.md',symbol:null}],history:[{sha:'12345678901234567890',subject:'更新规格'}],drift:{level:'anchored',reasons:['函数范围被修改'],commits:[{sha:'abcdef1234567890',subject:'更新代码'}]}}
const props={csrfToken:'csrf',onUnauthorized:vi.fn()}
function show(component:React.ReactNode,path='/projects/p1?tab=spec'){return render(<MemoryRouter initialEntries={[path]}>{component}</MemoryRouter>)}
function Location(){return <output>{useLocation().pathname+useLocation().search}</output>}
beforeEach(()=>{api.mockReset();api.mockImplementation(async url=>url.includes('/node?')?node as never:{nodes:[node,{...node,path:'.spec/app/child/spec.md',title:'子规格',depth:1,drift:{...node.drift,level:'file'}}],drift_count:2} as never)})
afterEach(cleanup)
it('renders hierarchy, counts, drift badges and opens a selected node',async()=>{
 const view=show(<SpecTree projectId="p1" onUnauthorized={props.onUnauthorized}/>);await screen.findByText('2 节点 · 2 处 drift')
 expect(screen.getByText('文件级漂移')).toBeTruthy();expect(screen.getByText('锚定漂移')).toBeTruthy()
 expect((view.container.querySelectorAll('.spec-list li')[1] as HTMLElement).style.paddingInlineStart).toBe('16px')
 fireEvent.click(screen.getByRole('button',{name:/订单计算/}));await screen.findByText('金额必须精确')
 expect(api).toHaveBeenCalledWith('/api/v2/projects/p1/spec-tree/node?path=.spec%2Fapp%2Fspec.md',expect.anything())
})
it('shows all detail sections, symbols, commit subjects and safe markdown',()=>{
 const view=show(<SpecNodeDetail node={node}/>);for(const title of ['人签意图 · raw source','展开规格 · expanded','管辖文件','相关文件 · related','漂移状态','版本历史'])expect(screen.getByRole('heading',{name:title})).toBeTruthy()
 expect(screen.getByText('#calculate')).toBeTruthy();expect(screen.getByText('abcdef12')).toBeTruthy();expect(screen.getByText(/更新规格/)).toBeTruthy()
 expect(view.container.querySelector('strong')?.textContent).toBe('精确计算');expect(view.container.querySelector('script')).toBeNull();expect(screen.getByText('危险').getAttribute('href')).not.toMatch(/^javascript:/)
})
it('displays unverified separately and invalid parse reasons',()=>{show(<><SpecDriftBadge drift={{level:'none',unverified:true,commits:[],reasons:[]}}/><SpecNodeDetail node={{...node,status:'invalid',raw_source:'',errors:['非法 frontmatter']}}/></>);expect(screen.getByText('未验证')).toBeTruthy();expect(screen.getByRole('alert').textContent).toContain('非法');expect(screen.getByText('尚未人签，等待确认意图。')).toBeTruthy()})
it('links acceptance evidence directly to its node',()=>{show(<SpecEvidenceLink projectId="p1" path={node.path}/>);expect(screen.getByRole('link').getAttribute('href')).toBe('/projects/p1?tab=spec&node=.spec%2Fapp%2Fspec.md')})
it('configures with revision and generates an ordinary run with a stable retry key',async()=>{
 const project={workspace:'/workspace',auto_issues:false,auto_publish:false,id:'p1',name:'项目',repository:'owner/repo',base_branch:'main',checks:{},revision:4,spec_tree_enabled:true};const onSaved=vi.fn()
 api.mockRejectedValueOnce(new Error('网络失败')).mockResolvedValueOnce({id:'r1'} as never)
 show(<><SpecSettings {...props} project={project} onSaved={onSaved}/><Location/></>)
 fireEvent.click(screen.getByRole('button',{name:'生成规格树'}));await screen.findByText('网络失败');fireEvent.click(screen.getByRole('button',{name:'生成规格树'}));await screen.findByText('/runs/r1')
 expect(JSON.parse(String(api.mock.calls[0][1]?.body)).idempotency_key).toBe(JSON.parse(String(api.mock.calls[1][1]?.body)).idempotency_key)
 cleanup();api.mockResolvedValueOnce({...project,spec_tree_enabled:false,revision:5} as never)
 show(<SpecSettings {...props} project={project} onSaved={onSaved}/>);fireEvent.click(screen.getByRole('button',{name:'关闭规格树'}));await waitFor(()=>expect(onSaved).toHaveBeenCalled());expect(JSON.parse(String(api.mock.calls[2][1]?.body))).toEqual({enabled:false,revision:4})
})
it('keeps the verification run snapshot when fetching nodes and following evidence',async()=>{
 show(<><SpecTree projectId="p1" onUnauthorized={props.onUnauthorized}/><SpecEvidenceLink projectId="p1" path={node.path} runId="r1"/></>,'/projects/p1?tab=spec&node=.spec%2Fapp%2Fspec.md&run=r1')
 await screen.findByText('金额必须精确')
 expect(api).toHaveBeenCalledWith('/api/v2/projects/p1/spec-tree?run_id=r1',expect.anything())
 expect(api).toHaveBeenCalledWith('/api/v2/projects/p1/spec-tree/node?path=.spec%2Fapp%2Fspec.md&run_id=r1',expect.anything())
 expect(screen.getByText('查看对应规格节点').getAttribute('href')).toContain('&run=r1')
})
