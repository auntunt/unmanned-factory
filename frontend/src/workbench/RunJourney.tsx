import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { runEvidence, runGuidance, type RunView } from './run-guidance'
import { StatusBadge } from './ui'
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

export default function RunJourney({ run }: { run: Run }) {
  const guidance = runGuidance(run)
  const evidence = runEvidence(run)
  const currentStage = stageIndex[guidance.stage]
  const rawCandidate = (run as Run & { capability_candidate_id?: unknown }).capability_candidate_id ?? run.artifacts?.capability_candidate_id
  const candidate = typeof rawCandidate === 'string' && rawCandidate ? rawCandidate : null
  const runHref = (view: RunView) => `/runs/${encodeURIComponent(String(run.id))}?view=${view}`
  const recorded = [evidence.requirements, evidence.plan, evidence.execution, evidence.checks !== 'none', evidence.delivery]
  const complete = [evidence.requirements, evidence.plan, false, evidence.checks === 'passed', run.status === 'published' && evidence.delivery]
  const labels = [
    evidence.requirements ? '已记录需求' : '未记录',
    evidence.plan ? '已记录计划' : '未记录',
    evidence.execution ? '已记录任务与尝试' : '未记录',
    evidence.checks === 'passed' ? '检查通过' : evidence.checks === 'failed' ? '检查未通过' : evidence.checks === 'recorded' ? '已记录检查' : '未记录',
    evidence.delivery ? (run.status === 'published' ? '已发布' : '已记录交付') : '未记录',
  ]
  const terminal = run.status === 'published' || run.status === 'discarded' || run.status === 'cancelled'
  return <section className="wb-run-journey" aria-labelledby="run-journey-title">
    <div className="wb-run-journey-heading">
      <div><span className="wb-eyebrow">本次工程闭环</span><h2 id="run-journey-title">{guidance.summary}</h2></div>
      <StatusBadge status={run.status} />
    </div>
    <ol className="wb-run-journey-stages">
      {stages.map(({ name, view }, index) => {
        const current = !terminal && index === currentStage
        const state = complete[index] ? 'is-recorded' : recorded[index] ? 'is-evidenced' : ''
        const label = current ? guidance.label : labels[index]
        return <li className={`${state} ${current ? 'is-current' : ''}`} key={name} aria-current={current ? 'step' : undefined}>
          <Link to={runHref(view)} aria-label={`${name}：${label}`}>
            <span className="wb-run-step-number" aria-hidden="true">{complete[index] ? '✓' : recorded[index] ? '•' : String(index + 1).padStart(2, '0')}</span>
            <strong>{name}</strong><small>{label}</small>
          </Link>
        </li>
      })}
      <li className={candidate ? 'is-evidenced' : ''}>
        {candidate ? <Link to={`/capabilities?selected=${encodeURIComponent(candidate)}`} aria-label="能力沉淀：已留草稿"><span className="wb-run-step-number" aria-hidden="true">•</span><strong>能力沉淀</strong><small>已留草稿</small></Link> : <><span className="wb-run-step-number" aria-hidden="true">06</span><strong>能力沉淀</strong><small>未记录</small></>}
      </li>
    </ol>
    {candidate && <div className="wb-run-journey-feedback"><span>本次经验已保存为能力草稿；验证并启用后才可用于后续需求。</span><Link to={`/capabilities?selected=${encodeURIComponent(candidate)}`}>查看能力草稿 <span aria-hidden="true">→</span></Link></div>}
  </section>
}
