import { useState } from 'react'
import './requirement-confirmation.css'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { errorText, type PageProps } from './ui'

export interface SpecDraft { goal: string; screens: { name: string; purpose: string }[]; flows: string[]; data_model: string[]; non_goals: string[]; risks_assumptions: string[] }
export interface RecommendedSkill { id: string; version: number; reason: string }
export interface FidelityTarget { reference: string; basis: string; screens: { screen: string; layout: string[]; colors: string[]; components: string[]; interactions: string[] }[] }
const sections = { flows: '关键流程', data_model: '数据模型要点', non_goals: '明确的非目标', risks_assumptions: '风险与假设' } as const
const aspects = { layout: '布局要点', colors: '配色', components: '关键组件', interactions: '核心交互' } as const
export default function RequirementConfirmation({ run, canAct, onChanged, ...props }: PageProps & { run: Run; canAct: boolean; onChanged: (run: Run) => void }) {
  const [draft, setDraft] = useState(run.spec_draft!)
  const [target, setTarget] = useState(run.fidelity_target)
  const [selected, setSelected] = useState(run.recommended_skills || [])
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const renameScreen = (index: number, name: string) => {
    const previous = draft.screens[index].name
    setDraft({ ...draft, screens: draft.screens.map((s, i) => i === index ? { ...s, name } : s) })
    if (target) setTarget({ ...target, screens: target.screens.map(s => s.screen === previous ? { ...s, screen: name } : s) })
  }
  const removeScreen = (index: number) => {
    const name = draft.screens[index].name
    setDraft({ ...draft, screens: draft.screens.filter((_, i) => i !== index) })
    if (target) setTarget({ ...target, screens: target.screens.filter(s => s.screen !== name) })
  }
  const addScreen = () => {
    let name = `页面 ${draft.screens.length + 1}`
    while (draft.screens.some(s => s.name === name)) name += '（新增）'
    setDraft({ ...draft, screens: [...draft.screens, { name, purpose: '' }] })
    if (target) setTarget({ ...target, screens: [...target.screens, { screen: name, layout: [''], colors: [''], components: [''], interactions: [''] }] })
  }
  const submit = async (action: string) => {
    if (busy) return
    setBusy(true); setError('')
    try { onChanged(await request<Run>(`/api/v2/runs/${encodeURIComponent(run.id)}/confirm-spec`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { revision: run.revision, action, spec_draft: draft, selected_skills: selected, fidelity_target: target || null } })) }
    catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  return <section className="wb-card" aria-label="确认需求后开工"><h2>确认理解，一次开工</h2><p>在同一屏核对规格、方法与保真标尺。开工后自动推进至交付；执行或验收问题仍会保留证据。</p>
    <form className="wb-form wb-requirement-form" onSubmit={e => { e.preventDefault(); void submit((e.nativeEvent as SubmitEvent).submitter?.getAttribute('value') || 'start') }}>
      <fieldset className="wb-requirement-spec" disabled={!canAct || busy}><legend>规格草案</legend>
        <label>目标<textarea required maxLength={4000} value={draft.goal} onChange={e => setDraft({ ...draft, goal: e.target.value })} /></label>
        <h3>屏／页清单</h3>{draft.screens.map((screen, i) => <div key={i}><label>页面 {i + 1} 名称<input required value={screen.name} onChange={e => renameScreen(i, e.target.value)} /></label><label>页面 {i + 1} 用途<input required value={screen.purpose} onChange={e => setDraft({ ...draft, screens: draft.screens.map((s, j) => j === i ? { ...s, purpose: e.target.value } : s) })} /></label><button type="button" className="wb-button wb-button-secondary" disabled={!!target && draft.screens.length === 1} onClick={() => removeScreen(i)}>移除页面 {i + 1}</button></div>)}
        <button type="button" className="wb-button wb-button-secondary" disabled={draft.screens.length >= 24} onClick={addScreen}>添加页面</button>
        {Object.entries(sections).map(([key, label]) => <label key={key}>{label}<textarea value={draft[key as keyof typeof sections].join('\n')} onChange={e => setDraft({ ...draft, [key]: e.target.value.split('\n') })} /></label>)}
      </fieldset>
      <fieldset className="wb-requirement-skills" disabled={!canAct || busy}><legend>推荐 skill（本次挂载）</legend>{(run.recommended_skills || []).map(skill => <label key={skill.id}><input type="checkbox" checked={selected.some(s => s.id === skill.id)} onChange={e => setSelected(e.target.checked ? [...selected, skill] : selected.filter(s => s.id !== skill.id))} /><span>{run.requirement_skill_catalog?.find(s => s.id === skill.id)?.name || skill.id} · v{skill.version}<small>{skill.reason}</small></span></label>)}{!run.recommended_skills?.length && <p>没有推荐匹配项，本次不自动添加 skill。</p>}</fieldset>
      {target && <fieldset disabled={!canAct || busy}><legend>保真标尺</legend><label>参考产品<input value={target.reference} onChange={e => setTarget({ ...target, reference: e.target.value })} /></label><p>{target.basis}</p>{target.screens.map((screen, i) => <div key={i}><h3>{screen.screen}</h3>{Object.entries(aspects).map(([key,label]) => <label key={key}>{screen.screen} · {label}<textarea value={screen[key as keyof typeof aspects].join('\n')} onChange={e => setTarget({ ...target, screens: target.screens.map((s,j) => j === i ? {...s,[key]:e.target.value.split('\n')} : s) })}/></label>)}</div>)}</fieldset>}
      {error && <p role="alert">{error}</p>}{canAct && <div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy}>开工</button><button type="submit" value="edit_start" className="wb-button wb-button-secondary" disabled={busy}>编辑后开工</button><button type="submit" value="waive" className="wb-button wb-button-secondary" disabled={busy}>放行</button></div>}
      <small>放行同样保存当前规格与勾选项，不会跳过独立验收。需求分析使用独立预算。</small>
    </form>
  </section>
}

export function BudgetResume({ run, onChanged, ...props }: PageProps & { run: Run; onChanged: (run: Run) => void }) {
 const [busy,setBusy]=useState(false),[error,setError]=useState('')
 const analysis = !run.spec_confirmation && !run.plan && (run.source?.operation === 'general' || run.source?.requirement_analysis === true)
 return <section className="wb-card"><p>{analysis ? '追加独立分析额度并重新分析需求；完成后仍需确认规格，不会跳过分析进入编码。' : '按项目当前额度续开本轮预算，保留已有费用与工作现场；已有可验收成果时直接进入验收。'}</p><button className="wb-button wb-button-primary" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{onChanged(await request<Run>(`/api/v2/runs/${encodeURIComponent(run.id)}/resume-budget`,{method:'POST',csrfToken:props.csrfToken,onUnauthorized:props.onUnauthorized,body:{revision:run.revision,resume_count:run.resume_count||0}}))}catch(e){setError(errorText(e))}finally{setBusy(false)}}}>{analysis ? '追加额度并重新分析' : '续跑并进入下一阶段'}</button>{error&&<p role="alert">{error}</p>}</section>
}
