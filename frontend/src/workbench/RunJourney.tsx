import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { StatusBadge } from './ui'
import './run-journey.css'

const stageNames = ['需求澄清', '方案规划', '开发执行', '质量验证', '交付发布', '能力沉淀']
const currentStages: Record<string, number> = {
  received: 0, needs_clarification: 0, planning: 1, awaiting_approval: 1,
  queued: 2, running: 2, verifying: 3, ready_for_review: 4, publishing: 4, published: 4,
}
const descriptions: Record<string, string> = {
  received: '需求已接收，等待分析目标与执行边界。',
  needs_clarification: '补充下方业务问题后，系统会重新规划并继续推进。',
  planning: '正在形成任务分工、依赖关系和验收方式。',
  awaiting_approval: '计划已经形成，等待确认本次执行范围。',
  queued: '任务已排队，等待执行资源。',
  running: '正在执行开发任务，模型尝试和修复过程记录在下方。',
  verifying: '正在检查集成结果，检查通过后形成可交付产物。',
  ready_for_review: '代码已通过配置的检查，尚未发布交付。',
  publishing: '检查已通过，正在发布交付产物。',
  published: '交付已发布，可查看交付证据和来源记录。',
  needs_human: '运行已暂停，需要补充信息或处理异常后继续。',
  failed: '本次运行失败。查看失败证据后，可以创建关联重试。',
  cancelled: '本次运行已取消，已产生的记录与证据仍可查看。',
}

export default function RunJourney({ run }: { run: Run }) {
  const stage = currentStages[run.status]
  const rawCandidate = (run as Run & { capability_candidate_id?: unknown }).capability_candidate_id ?? run.artifacts?.capability_candidate_id
  const candidate = typeof rawCandidate === 'string' && rawCandidate ? rawCandidate : null
  const progressLabels = ['已明确', '已形成', '已执行', '已通过', '已发布', '已留草稿']
  const waitingLabels = ['待澄清', '待规划', '待执行', '待验证', '待发布', '待沉淀']
  return <section className="wb-run-journey" aria-labelledby="run-journey-title">
    <div className="wb-run-journey-heading">
      <div><span className="wb-eyebrow">本次工程闭环</span><h2 id="run-journey-title">{descriptions[run.status]}</h2></div>
      <StatusBadge status={run.status} />
    </div>
    <ol className="wb-run-journey-stages">
      {stageNames.map((name, index) => {
        const done = index === 5 ? Boolean(candidate) : stage !== undefined && (index < stage || index === 4 && run.status === 'published')
        const current = index === stage && !done
        let label = done ? progressLabels[index] : waitingLabels[index]
        if (current) label = run.status === 'ready_for_review' ? '待发布' : run.status === 'queued' ? '排队中' : run.status === 'awaiting_approval' || run.status === 'needs_clarification' ? '待确认' : '进行中'
        if (stage === undefined && !done) label = ['已接收', run.plan ? '有计划记录' : '未记录计划', '查看尝试记录', '查看检查记录', '未发布', '待沉淀'][index]
        return <li className={`${done ? 'is-recorded' : ''} ${current ? 'is-current' : ''}`} key={name} aria-current={current ? 'step' : undefined}>
          <span className="wb-run-step-number" aria-hidden="true">{done ? '✓' : String(index + 1).padStart(2, '0')}</span>
          <strong>{name}</strong><small>{label}</small>
        </li>
      })}
    </ol>
    {candidate && <div className="wb-run-journey-feedback"><span>↳ 本次经验已保存为能力草稿，验证并启用后可用于后续需求。</span><Link to={`/capabilities?selected=${encodeURIComponent(candidate)}`}>查看能力草稿 <span aria-hidden="true">→</span></Link></div>}
  </section>
}
