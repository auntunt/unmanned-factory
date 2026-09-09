import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import EngineeringLoop from './EngineeringLoop'
import { PROJECT_STAGES, projectStage, projectStageHref } from './project-stages'
import { formatDate, StatusBadge, statusLabel } from './ui'
import type { EngineeringSummary } from './v3-types'
import { checkPassed, runGuidance, verificationScopeNote } from './run-guidance'

interface Props {
  projectId: string | number
  projectName: string
  engineering: EngineeringSummary
  runs: Run[]
  selectedStage: string
  onSelect: (id: string) => void
  requirementForm: ReactNode
  isAdmin: boolean
}

type RecordValue = Record<string, unknown>
const records = (value: unknown): RecordValue[] => Array.isArray(value) ? value.filter((item): item is RecordValue => Boolean(item && typeof item === 'object' && !Array.isArray(item))) : []

function taskRecords(run: Run): RecordValue[] {
  return records(run.tasks).length ? records(run.tasks) : records(run.artifacts?.tasks)
}

function checkRecords(run: Run): RecordValue[] {
  const integrated = records(run.artifacts?.checks)
  if (integrated.length) return integrated
  return taskRecords(run).flatMap((task) => {
    const attempts = records(task.attempts)
    return records(task.checks).length ? records(task.checks) : records(attempts[attempts.length - 1]?.checks)
  })
}

function StagePreview({ stage, run }: { stage: string; run: Run }) {
  if (stage === 'intake') return <>
    <p>{run.request}</p>
    {(run.triage?.questions ?? []).length > 0 && <ul className="pw-record-list">{run.triage!.questions.slice(0, 3).map((question) => <li key={question}><strong>{question}</strong></li>)}</ul>}
  </>
  if (stage === 'plan') return <>
    <p>{run.plan?.summary || '正在生成方案，尚未返回完整计划。'}</p>
    <ul className="pw-record-list">{run.plan?.tasks.slice(0, 3).map((task) => <li key={task.id}><strong>{task.title}</strong><span>{run.execution_mode === 'continuous' ? '持续编码与自测' : task.depends_on.length ? `依赖 ${task.depends_on.join('、')}` : '可独立执行'}</span></li>)}</ul>
    {run.plan && <p>{run.execution_mode === 'continuous' ? '持续编码目标' : `${run.plan.tasks.length} 项任务`} · 计划版本 {run.revision}</p>}
  </>
  if (stage === 'build') {
    const tasks = taskRecords(run)
    return <><p>{runGuidance(run).summary}</p><ul className="pw-record-list">{tasks.slice(0, 4).map((task, index) => <li key={String(task.id ?? index)}><strong>{String(task.title ?? task.id ?? `任务 ${index + 1}`)}</strong><span>{statusLabel(String(task.status ?? 'pending'))} · {records(task.attempts).length} 次尝试</span></li>)}</ul>{!tasks.length && <p>等待执行器返回任务与尝试记录。</p>}</>
  }
  if (stage === 'verify') {
    const checks = checkRecords(run)
    return checks.length ? <><p>{verificationScopeNote(run)}</p><ul className="pw-record-list">{checks.slice(0, 5).map((check, index) => {
      const passed = checkPassed(check)
      return <li key={index}><strong>{String(check.name ?? `检查 ${index + 1}`)}</strong><span className={passed ? 'is-pass' : 'is-fail'}>{passed ? '通过' : check.timeout ? '超时' : '未通过'}</span></li>
    })}</ul></> : <p>正在检查，尚未记录检查输出。结果返回后会显示在这里。</p>
  }
  if (stage === 'deliver') return <div className="pw-delivery-values">
    <span>交付状态：{run.status === 'published' ? '已发布' : run.status === 'publishing' ? '正在发布' : '尚未发布'}</span>
    {typeof run.artifacts?.commit === 'string' && <span>代码提交<code>{run.artifacts.commit}</code></span>}
    {typeof run.artifacts?.branch === 'string' && <span>交付分支<code>{run.artifacts.branch}</code></span>}
    {typeof run.artifacts?.pr_url === 'string' && <span>发布记录<code>{run.artifacts.pr_url}</code></span>}
  </div>
  return null
}

export default function ProjectLifecycle({ projectId, projectName, engineering, runs, selectedStage, onSelect, requirementForm, isAdmin }: Props) {
  const stage = projectStage(selectedStage)
  const stages = PROJECT_STAGES.map((definition) => engineering.stages.find((item) => item.id === definition.id)
    ?? { ...definition, count: 0, unit: '条记录', items: [] })
  const content = stages.find((item) => item.id === stage.id)!
  const previous = PROJECT_STAGES[Math.max(0, PROJECT_STAGES.findIndex((item) => item.id === stage.id) - 1)]
  return <section className="wb-card pw-cycle" aria-labelledby="project-cycle-title">
    <div className="pw-cycle-heading"><div><span className="wb-eyebrow">{projectName} · 工程闭环</span><h2 id="project-cycle-title">这个项目的工作与产出</h2><p>六环组织需求、执行与成果记录，不要求每环单独调用模型；已有记录数不代表完成率。</p></div><Link className="wb-text-link" to={`/runs?project_id=${encodeURIComponent(String(projectId))}`}>本项目全部运行 →</Link></div>
    <div className="pw-cycle-layout">
      <EngineeringLoop stages={stages} selectedId={stage.id} onSelect={onSelect} />
      <section className="pw-stage-panel" id="ov3-stage-details" aria-labelledby="project-stage-title">
        <div className="pw-stage-heading"><h3 id="project-stage-title">{stage.label}</h3><span>{content.count} {content.unit}</span></div>
        <p className="pw-stage-description">{stage.description}</p>
        {stage.id === 'intake' && requirementForm}
        {content.items.map((item) => {
          const run = runs.find((record) => String(record.id) === item.id)
          return <article className="pw-stage-record" key={item.id}>
            <div className="pw-record-meta"><span>{formatDate(item.updated_at)}</span>{run ? <span>{runGuidance(run).label}</span> : <StatusBadge status={item.status} />}</div>
            <h4><Link to={item.href}>{item.title}</Link></h4>
            {run ? <StagePreview stage={stage.id} run={run} /> : <p>{item.detail}</p>}
            <Link className="wb-text-link" to={item.href}>{stage.action} →</Link>
          </article>
        })}
        {content.items.length === 0 && <div className="pw-stage-empty"><h4>{stage.empty}</h4>
          <p>{stage.id === 'reuse' ? '交付留下的能力草稿会归到这里。已验证的能力可以用于这个项目的后续需求。' : stage.id === 'intake' ? '写下要完成的业务目标；不明确的地方，工厂会先提问。' : `先在“${previous.label}”中推进工作，有了${stage.label === '质量验证' ? '实际检查结果' : '对应产出'}后，这里会自动更新。`}</p>
          {stage.id !== 'intake' && <Link className="wb-text-link" to={projectStageHref(projectId, previous.id)}>查看本项目{previous.label} →</Link>}
        </div>}
        {content.count > content.items.length && <p className="pw-stage-tail">最近展示 {content.items.length} 条，共 {content.count} {content.unit}。<Link to={`/runs?project_id=${encodeURIComponent(String(projectId))}`}>浏览本项目完整运行记录 →</Link></p>}
        {stage.id === 'reuse' && isAdmin && <p className="pw-stage-tail"><Link className="wb-text-link" to={`/projects/${encodeURIComponent(String(projectId))}?tab=automation`}>管理本项目绑定的能力 →</Link><br />跨项目复用时，选择并绑定已启用的具体版本。</p>}
        {stage.id === 'verify' && isAdmin && <p className="pw-stage-tail"><Link className="wb-text-link" to={`/projects/${encodeURIComponent(String(projectId))}?tab=settings#project-checks`}>配置本项目的验收检查 →</Link></p>}
      </section>
    </div>
  </section>
}
