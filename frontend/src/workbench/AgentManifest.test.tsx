// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { request } from '../workspace/api'
import AgentManifest from './AgentManifest'
vi.mock('../workspace/api',async original=>({...await original<typeof import('../workspace/api')>(),request:vi.fn()}))
const api=vi.mocked(request)
const m={revision:2,identity:'维护职责',skills:[{id:'s1',version:1}],assertions:['报告存在'],resolved_skills:[{id:'s1',version:1,name:'回归检查',source:{type:'legacy'}}],history:[{revision:1,identity:'原职责',skills:[],assertions:[]}]}
const props={csrfToken:'csrf',onUnauthorized:vi.fn(),user:{id:1,username:'admin',role:'admin' as const}}
beforeEach(()=>{api.mockReset();api.mockImplementation(async(path)=>path==='/api/v4/modules'?{modules:[{id:'s1',version:2,name:'回归检查'},{id:'s2',version:1,name:'日志检查'}]}:m)})
afterEach(cleanup)
it('shows pinned skills, source, assertions and saves a human manifest with revision',async()=>{render(<MemoryRouter><AgentManifest agentId="a1" {...props}/></MemoryRouter>);await screen.findByRole('link',{name:'回归检查'});expect(screen.getByText(/来源：legacy/)).toBeTruthy();fireEvent.change(screen.getByLabelText('身份段'),{target:{value:'新职责'}});fireEvent.change(screen.getByLabelText('加入 skill'),{target:{value:'s2'}});fireEvent.click(screen.getByRole('button',{name:'保存清单'}));await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v4/agents/a1/manifest',expect.objectContaining({method:'PUT',body:expect.objectContaining({revision:2,identity:'新职责',skills:[{id:'s1',version:1},{id:'s2',version:1}]})})));expect(screen.getByRole('link',{name:'导出职能包 v2 ZIP'}).getAttribute('href')).toContain('/a1/pack')})
it('restores history as a new revision, rather than changing the old entry',async()=>{render(<MemoryRouter><AgentManifest agentId="a1" {...props}/></MemoryRouter>);fireEvent.click(await screen.findByText('清单历史'));fireEvent.click(screen.getByRole('button',{name:'恢复 revision 1'}));await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v4/agents/a1/manifest/restore',expect.objectContaining({body:{revision:2,target_revision:1}})))})
it('member sees manifest without write controls',async()=>{render(<MemoryRouter><AgentManifest agentId="a1" {...props} user={{...props.user,role:'member'}}/></MemoryRouter>);await screen.findByLabelText('身份段');expect((screen.getByLabelText('身份段') as HTMLTextAreaElement).disabled).toBe(true);expect(screen.queryByRole('button',{name:'保存清单'})).toBeNull()})
