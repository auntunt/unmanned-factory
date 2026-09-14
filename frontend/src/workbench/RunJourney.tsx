import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { runEvidence, runGuidance, type RunView } from './run-guidance'
import { tabKeys } from './presentation'
import './run-journey.css'
import './run-guidance.css'

const stages: Array<{ name: string; view: RunView }> = [
  { name: '需求澄清', view: 'requirements' },
  { name: '方案规划', view: 'plan' },
  { name: '开发执行', view: 'execution' },
  { name: '质量验证', view: 'verification' },
  { name: '交付发布', view: 'delivery' },
]
const stageIndex = { intake: 0, plan: 1, build: 2, verify: 3, deliver: 4 } as const

export default function RunJourney({ run, activeView, guidanceContent }: { run: Run; activeView?: RunView; guidanceContent?: ReactNode }) {
  const guidance = runGuidance(run)
  const evidence = runEvidence(run)
  const currentStage = stageIndex[guidance.stage]
  const rawCandidate = (run as Run & { capability_candidate_id?: unknown }).capability_candidate_id ?? run.artifacts?.capability_candidate_id
  const candidate = typeof rawCandidate === 'string' && rawCandidate ? rawCandidate : null
  const runHref = (view: RunView) => `/runs/${encodeURIComponent(String(run.id))}?view=${view}`
  const recorded = [evidence.requirements, evidence.plan, evidence.execution, evidence.checks !== 'none', evidence.delivery]
  const complete = [evidence.requirements, evidence.plan, false, evidence.checks === 'passed' && ['ready_for_review', 'publishing', 'published'].includes(run.status), run.status === 'published' && evidence.delivery]
  const labels = [
    evidence.requirements ? '已记录需求' : '未记录',
    evidence.plan ? '已记录计划' : '未记录',
    evidence.execution ? '已记录任务与尝试' : '未记录',
    evidence.checks === 'passed' ? (['ready_for_review', 'publishing', 'published'].includes(run.status) ? '验证通过' : '已有检查记录，待整体验证') : evidence.checks === 'unverified' ? '未验证' : evidence.checks === 'failed' ? '检查未通过' : evidence.checks === 'recorded' ? '已记录检查' : '未记录',
    evidence.delivery ? (run.status === 'published' ? '已发布' : '已记录交付') : '未记录',
  ]
  const terminal = run.status === 'published' || run.status === 'discarded' || run.status === 'cancelled'
  return <section className="wb-run-journey" aria-labelledby="run-journey-title">
    {guidanceContent}
    <div className="wb-run-journey-heading"><h2 id="run-journey-title">本次工程闭环</h2></div>
    <ol className="wb-run-journey-stages" role="tablist" aria-label="运行工作视图" onKeyDown={tabKeys}>
      {stages.map(({ name, view }, index) => {
        const current = !terminal && index === currentStage
        const state = complete[index] ? 'is-recorded' : recorded[index] ? 'is-evidenced' : ''
        const label = current ? guidance.label : labels[index]
        return <li className={`${state} ${current ? 'is-current' : ''}`} key={name} role="presentation">
          <Link role="tab" id={`run-tab-${view}`} aria-controls="run-view-panel" aria-selected={(activeView ?? guidance.view) === view} tabIndex={(activeView ?? guidance.view) === view ? 0 : -1} to={runHref(view)} aria-label={`${name}：${label}`}>
            <span className="wb-run-step-number" aria-hidden="true">{complete[index] ? '✓' : recorded[index] ? '•' : String(index + 1).padStart(2, '0')}</span>
            <strong>{name}</strong><small>{label}</small>
          </Link>
        </li>
      })}
    </ol>
    <p className="wb-runtime-note">能力沉淀（可选） · 按需整理</p>
    {candidate && <div className="wb-run-journey-feedback"><span>有值得复用的经验时，可查看并启用草稿；不沉淀也不影响本次交付。</span><Link to={`/capabilities?selected=${encodeURIComponent(candidate)}`}>查看能力草稿 </Link></div>}
  </section>
}
