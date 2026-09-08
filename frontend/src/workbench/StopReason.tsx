import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { runGuidance } from './run-guidance'
import './run-guidance.css'

/** Shows a persisted stop reason together with only the next actions the viewer may take. */
export function StopReason({ run, canConfigure }: { run: Run; canConfigure: boolean }) {
  const guidance = runGuidance(run)
  if (!['budget', 'billing', 'failure', 'recovery', 'paused'].includes(guidance.kind)) return null
  const projectHref = `/projects/${encodeURIComponent(String(run.project_id))}?stage=${guidance.stage}`
  const budgetHref = `/projects/${encodeURIComponent(String(run.project_id))}?tab=settings&return_run=${encodeURIComponent(String(run.id))}#project-budget`
  const adminActions = guidance.kind === 'budget'
    ? [{ href: '/settings/runtime', label: '查看模型策略' }, { href: '/team', label: '查看团队额度' }]
    : guidance.kind === 'billing'
      ? [{ href: '/team', label: '查看团队额度' }, { href: '/settings/runtime', label: '查看模型策略' }]
      : guidance.kind === 'recovery' || guidance.kind === 'paused'
        ? [{ href: '/settings/runtime', label: '查看模型策略' }]
        : []
  const tone = guidance.kind === 'failure' ? 'is-danger' : 'is-warning'
  return (
    <section className={`wb-stop-reason wb-guidance ${tone}`} role="alert" aria-labelledby="stop-reason-title">
      <div className="wb-detail-kicker">当前需要处理</div>
      <h2 id="stop-reason-title">{guidance.label}</h2>
      <p>{guidance.summary}</p>
      {guidance.kind === 'budget' && <p className="wb-stop-reason-admin-note">这里显示的是本次运行使用的预算。调整项目预算后，可点击“按新配置重试”；保存设置不会自动继续这次运行。</p>}
      <div className="wb-stop-reason-actions">
        {guidance.kind === 'budget' && canConfigure ? <Link to={budgetHref}>调整项目预算</Link> : <Link to={guidance.primaryHref}>{guidance.primaryLabel}</Link>}
        <Link to={projectHref}>返回项目</Link>
        {canConfigure && adminActions.map((action) => <Link key={action.href} to={action.href}>{action.label}</Link>)}
      </div>
      {!canConfigure && adminActions.length > 0 && <p className="wb-stop-reason-admin-note">你可以查看运行和项目记录；预算、模型策略和团队额度由管理员配置，请联系管理员处理。</p>}
      {guidance.rawEvidence && <details className="wb-stop-reason-raw"><summary>后端原始信息</summary><code>{guidance.rawEvidence}</code></details>}
    </section>
  )
}
