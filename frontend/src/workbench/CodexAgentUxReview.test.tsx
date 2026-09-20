// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import AgentsPage from './AgentsPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
vi.mock('./AgentManifest', () => ({default: () => <div>规范</div>}))
vi.mock('./AgentEvolution', () => ({default: () => null}))
const api=vi.mocked(request)
const a={id:'a1',name:'助手甲',purpose:'甲用途',active_version:1,updated_at:'2026-09-20T00:00:00Z'}
const b={...a,id:'a2',name:'助手乙',purpose:'乙用途'}
const props={csrfToken:'csrf',onUnauthorized:vi.fn(),user:{id:1,username:'admin',role:'admin' as const}}
function setup(environment?: unknown) {
 api.mockReset()
 api.mockImplementation(async(path:string) => {
  if(path==='/api/v4/agents') return {agents:[a,b]} as never
  if(path==='/api/v4/agents/a1') return a as never
  if(path==='/api/v4/agents/a2') return b as never
  if(path.includes('/preflight')) return {ready:true,message:''} as never
  if(path.includes('/bindings/')) return {bindings:[{id:'b1',pack_id:'p1',pack_name:'现有工具',version_id:'v1',version:1,revision:1,environment}]} as never
  if(path==='/api/v4/capability-packs') return {packs:[{id:'p2',name:'待添加工具',purpose:'新工具',published_version:1}]} as never
  if(path.includes('/modules'))return {modules:[]} as never
  if(path.includes('/capabilities'))return {capabilities:[]} as never
  return {items:[],skills:[],proposals:[],conversations:[],versions:[]} as never
 })
}
function Switch(){ const navigate=useNavigate(); return <button onClick={()=>navigate('/agents/a2')}>切换到乙</button> }
function show(){render(<MemoryRouter initialEntries={['/agents/a1']}><Switch/><Routes><Route path='/agents/:agentId' element={<AgentsPage {...props}/>}/></Routes></MemoryRouter>)}
afterEach(cleanup)
it.each([{status:'unchecked'},{status:'checking'},undefined])('never says ready for environment %j',async(env)=>{
 setup(env);show();await screen.findByText('现有工具'); expect(screen.queryByText('环境就绪')).toBeNull()
})
it('navigation to another agent does not retain previous agent editor and bindings',async()=>{
 setup();show();await screen.findByRole('heading',{name:'助手甲'});fireEvent.click(screen.getByRole('button',{name:'切换到乙'}));await screen.findByRole('heading',{name:'助手乙'});expect(screen.queryByRole('heading',{name:'助手甲'})).toBeNull()
})
it('team add flow exposes an available unbound tool in the current agent context',async()=>{
 setup();show();await screen.findByRole('region',{name:'添加能力'});fireEvent.click(screen.getByRole('button',{name:'团队已有能力'}));await screen.findByText('待添加工具')
})
