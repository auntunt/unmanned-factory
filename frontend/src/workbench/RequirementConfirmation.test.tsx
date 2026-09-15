// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import RequirementConfirmation, { BudgetResume } from './RequirementConfirmation'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
vi.mock('../workspace/api',async original=>({...await original<typeof import('../workspace/api')>(),request:vi.fn()}))
const run={id:'run',project_id:'project',revision:1,status:'awaiting_spec_confirmation',spec_draft:{goal:'团购工具',screens:[{name:'首页',purpose:'浏览商品'}],flows:['选择分类'],data_model:['商品'],non_goals:['不支付'],risks_assumptions:['示例数据']},recommended_skills:[{id:'style',version:1,reason:'匹配界面风格'}],requirement_skill_catalog:[{id:'style',version:1,name:'界面规范'}],fidelity_target:{reference:'美团',basis:'模型知识，未抓取',screens:[{screen:'首页',layout:['搜索与分类'],colors:['黄色'],components:['商品卡片'],interactions:['筛选']}]}} as unknown as Run
const props={csrfToken:'csrf',onUnauthorized:vi.fn(),onChanged:vi.fn()}
afterEach(()=>{cleanup();vi.clearAllMocks()})
it('confirms edited spec, selected skills and fidelity in exactly one request',async()=>{
 vi.mocked(request).mockResolvedValue({...run,status:'received'})
 render(<RequirementConfirmation {...props} run={run} canAct />)
 expect(screen.getByRole('group',{name:'规格草案'})).toBeTruthy()
 expect(screen.getByRole('group',{name:'保真标尺'})).toBeTruthy()
 expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true)
 fireEvent.change(screen.getByLabelText('目标'),{target:{value:'修改后的目标'}})
 fireEvent.click(screen.getByRole('checkbox'))
 fireEvent.click(screen.getByRole('button',{name:'编辑后开工'}))
 await waitFor(()=>expect(props.onChanged).toHaveBeenCalled())
 expect(request).toHaveBeenCalledTimes(1)
 expect(request).toHaveBeenCalledWith('/api/v2/runs/run/confirm-spec',expect.objectContaining({body:expect.objectContaining({revision:1,action:'edit_start',spec_draft:expect.objectContaining({goal:'修改后的目标'}),selected_skills:[],fidelity_target:run.fidelity_target})}))
})
it('keeps the spec visible on conflict and prevents unauthorized confirmation',async()=>{
 vi.mocked(request).mockRejectedValue(new Error('规格版本已变化'))
 const view=render(<RequirementConfirmation {...props} run={run} canAct />)
 fireEvent.click(screen.getByRole('button',{name:'放行'}))
 expect((await screen.findByRole('alert')).textContent).toContain('规格版本已变化')
 expect(props.onChanged).not.toHaveBeenCalled()
 view.rerender(<RequirementConfirmation {...props} run={run} canAct={false}/>)
 expect(screen.queryByRole('button',{name:'开工'})).toBeNull()
})
it('offers a one-click budget continuation using the current revision and resume count',async()=>{
 vi.mocked(request).mockResolvedValue({...run,status:'queued'})
 render(<BudgetResume {...props} run={{...run,resume_count:2}} />)
 fireEvent.click(screen.getByRole('button',{name:'续跑并进入下一阶段'}))
 await waitFor(()=>expect(props.onChanged).toHaveBeenCalled())
 expect(request).toHaveBeenCalledWith('/api/v2/runs/run/resume-budget',expect.objectContaining({body:{revision:1,resume_count:2}}))
})
it('keeps fidelity screens in sync when the owner changes the page list',async()=>{
 vi.mocked(request).mockResolvedValue({...run,status:'received'})
 render(<RequirementConfirmation {...props} run={run} canAct />)
 fireEvent.change(screen.getByLabelText('页面 1 名称'),{target:{value:'发现页'}})
 expect(screen.getByLabelText('发现页 · 布局要点')).toBeTruthy()
 fireEvent.click(screen.getByRole('button',{name:'添加页面'}))
 expect(screen.getByLabelText('页面 2 · 配色')).toBeTruthy()
 fireEvent.click(screen.getByRole('button',{name:'移除页面 2'}))
 fireEvent.click(screen.getByRole('button',{name:'开工'}))
 await waitFor(()=>expect(request).toHaveBeenCalled())
 expect(vi.mocked(request).mock.calls[0][1]?.body).toEqual(expect.objectContaining({spec_draft:expect.objectContaining({screens:[{name:'发现页',purpose:'浏览商品'}]}),fidelity_target:expect.objectContaining({screens:[expect.objectContaining({screen:'发现页'})]})}))
})
