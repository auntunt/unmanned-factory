// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { request } from '../workspace/api'
import { RunSpecReferences, SpecReferenceInput } from './SpecReferences'
vi.mock('../workspace/api',async original=>({...await original<typeof import('../workspace/api')>(),request:vi.fn()}))
const api=vi.mocked(request);const onUnauthorized=vi.fn()
function Editor({enabled=true}:{enabled?:boolean}){const [value,setValue]=useState('');return <label>需求<SpecReferenceInput id="request" projectId="p1" enabled={enabled} value={value} onChange={setValue} onUnauthorized={onUnauthorized}/></label>}
beforeEach(()=>{api.mockReset();api.mockResolvedValue({nodes:[{path:'.spec/orders/spec.md',title:'订单'},{path:'.spec/users/spec.md',title:'账户'}]} as never);vi.stubGlobal('requestAnimationFrame',(f:()=>void)=>{f();return 1})})
afterEach(()=>{cleanup();vi.unstubAllGlobals()})
it('completes a reference with keyboard and keeps surrounding text',async()=>{render(<Editor/>);const input=screen.getByRole('textbox');fireEvent.change(input,{target:{value:'修复 [[订',selectionStart:7}});await screen.findByRole('option',{name:/订单/});fireEvent.keyDown(input,{key:'Enter'});expect((input as HTMLTextAreaElement).value).toBe('修复 [[订单]]');expect(screen.queryByRole('listbox')).toBeNull()})
it('supports clicking and escape, excludes ambiguous identifiers',async()=>{api.mockResolvedValueOnce({nodes:[{path:'.spec/a/same/spec.md',title:'重复'},{path:'.spec/b/same/spec.md',title:'重复'},{path:'.spec/unique/spec.md',title:'唯一'}]} as never);render(<Editor/>);const input=screen.getByRole('textbox');fireEvent.change(input,{target:{value:'[[',selectionStart:2}});await screen.findByRole('option',{name:/唯一/});expect(screen.queryByRole('option',{name:/重复/})).toBeNull();fireEvent.keyDown(input,{key:'Escape'});expect(screen.queryByRole('listbox')).toBeNull();fireEvent.change(input,{target:{value:'[[唯',selectionStart:3}});fireEvent.click(screen.getByRole('option',{name:/唯一/}));expect((input as HTMLTextAreaElement).value).toBe('[[唯一]]')})
it('does not fetch or change textbox behavior for disabled projects',()=>{render(<Editor enabled={false}/>);fireEvent.change(screen.getByRole('textbox'),{target:{value:'[[任意]]'}});expect(api).not.toHaveBeenCalled();expect(screen.queryByRole('listbox')).toBeNull()})
it('keeps typing available after a failed suggestion request',async()=>{api.mockRejectedValueOnce(new Error('offline'));render(<Editor/>);await screen.findByText(/节点建议暂不可用/);fireEvent.change(screen.getByRole('textbox'),{target:{value:'继续工作'}});expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('继续工作')})
it('shows resolved links, frozen commits and unmatched references',()=>{render(<MemoryRouter><RunSpecReferences projectId="p1" source={{spec_refs:[{name:'订单',title:'订单',path:'.spec/orders/spec.md',commit:'abcdef123456'}],spec_ref_unmatched:[{name:'重复',reason:'ambiguous'}]}}/></MemoryRouter>);expect(screen.getByRole('link',{name:'订单'}).getAttribute('href')).toBe('/projects/p1?tab=spec&node=.spec%2Forders%2Fspec.md');expect(screen.getByText('abcdef12')).toBeTruthy();expect(screen.getByText('未匹配规格节点')).toBeTruthy();expect(screen.getByText(/存在多个同名节点/)).toBeTruthy()})
