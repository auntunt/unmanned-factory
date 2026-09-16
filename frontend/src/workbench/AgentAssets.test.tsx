// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,render,screen} from '@testing-library/react'
import AgentAssets from './AgentAssets'
import {request} from '../workspace/api'
vi.mock('../workspace/api',()=>({request:vi.fn()}))
afterEach(cleanup)
it('keeps the uploaded filename and source inventory visible without claiming it is enabled',async()=>{
  vi.mocked(request).mockResolvedValue({skills:[{id:'s',filename:'formats.zip',files:[{path:'formats/SKILL.md'}]}]})
  render(<AgentAssets agentId="a" refresh={0} csrfToken="csrf" onUnauthorized={()=>{}}/>)
  await screen.findByText('formats.zip · 1 个文件')
  expect(screen.getByText('formats/SKILL.md')).toBeTruthy()
  expect(screen.getByText(/不代表已经启用/)).toBeTruthy()
})
