import Icon from './Icon'
import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { runGuidance } from './run-guidance'
import './run-guidance.css'

/** Shows a persisted stop reason together with only the next actions the viewer may take. */
export function StopReason({ run, canConfigure, onContinue, onRetry, busy = false }: { run: Run; canConfigure: boolean; onContinue?: () => void; onRetry?: () => void; busy?: boolean }) {
  const guidance = runGuidance(run)
  const projectHref = `/projects/${encodeURIComponent(String(run.project_id))}?stage=${guidance.stage}`
  const budgetHref = `/projects/${encodeURIComponent(String(run.project_id))}?tab=settings&return_run=${encodeURIComponent(String(run.id))}#project-budget`
  const adminActions = guidance.kind === 'budget'
    ? [{ href: '/settings/runtime', label: '查看模型策略' }, { href: '/team', label: '查看团队额度' }]
    : guidance.kind === 'billing'
      ? [{ href: '/team', label: '查看团队额度' }, { href: '/settings/runtime', label: '查看模型策略' }]
      : guidance.kind === 'recovery' || guidance.kind === 'paused'
        ? [{ href: '/settings/runtime', label: '查看模型策略' }]
        : []
  const tone = guidance.kind === 'failure' ? 'is-danger' : ['budget', 'billing', 'recovery', 'paused'].includes(guidance.kind) ? 'is-warning' : ''
  return (
    <section className={`wb-stop-reason wb-guidance ${tone}`} role={tone ? 'alert' : 'status'} aria-labelledby="stop-reason-title">
      <div className="wb-detail-kicker">{tone ? '当前需要处理' : '当前进展'}</div>
      <h2 id="stop-reason-title">{guidance.label}</h2>
      <p>{guidance.summary}</p>
      {guidance.kind === 'budget' && <p className="wb-stop-reason-admin-note">这是上次暂停的原因，不代表项目当前仍有限额。保存额度设置后，点击“继续工作”接续原任务；没有可恢复现场时才需要创建新运行。</p>}
      <div className="wb-stop-reason-actions">
        {onContinue ? <button type="button" className="wb-button wb-button-primary" disabled={busy} onClick={onContinue}>{busy ? '正在处理…' : '继续工作'}</button> : onRetry ? <button type="button" className="wb-button wb-button-primary" disabled={busy} onClick={onRetry}>{busy ? '正在处理…' : '按当前配置重试'}</button> : null}
        {onContinue && <Link to={`/runs/${encodeURIComponent(String(run.id))}?view=execution#run-recovery`}>补充信息再继续</Link>}
        {guidance.kind === 'budget' && canConfigure ? <Link to={budgetHref}>调整项目预算</Link> : <Link to={['paused', 'budget', 'billing', 'recovery'].includes(guidance.kind) ? `${guidance.primaryHref}#run-recovery` : guidance.primaryHref}>{guidance.primaryLabel}</Link>}
        <Link to={projectHref}>返回项目</Link>
        {canConfigure && adminActions.map((action) => <Link key={action.href} to={action.href}>{action.label}</Link>)}
      </div>
      {!canConfigure && adminActions.length > 0 && <p className="wb-stop-reason-admin-note">你可以查看运行和项目记录；预算、模型策略和团队额度由管理员配置，请联系管理员处理。</p>}
      {guidance.rawEvidence && <details className="wb-stop-reason-raw"><summary><Icon name="triangle" className="wb-disclosure-icon" />后端原始信息</summary><code>{guidance.rawEvidence}</code></details>}
    </section>
  )
}
