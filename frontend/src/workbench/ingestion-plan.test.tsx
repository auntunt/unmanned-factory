// @vitest-environment jsdom
import {afterEach,expect,it} from 'vitest'
import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {PlanPanel} from './RunPage'
import type {Run} from '../workspace/types'
afterEach(cleanup)
it('renders a saved ingestion plan lacking dependency metadata in both views',()=>{
  const run={revision:1,plan:{summary:'职能包适配',tasks:[{id:'adapt',title:'职能包适配',prompt:'只读分类',acceptance:['保留来源'],paths:[],status:'pending'}]},tasks:[]} as unknown as Run
  render(<PlanPanel run={run}/>)
  expect(screen.getByRole('group',{name:'可交互任务依赖图'})).toBeTruthy()
  expect(screen.getByText('保留来源')).toBeTruthy()
  fireEvent.click(screen.getByRole('button',{name:'任务列表'}))
  expect(screen.getByText('验收：保留来源')).toBeTruthy()
})
