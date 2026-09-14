import type { Run } from '../workspace/types'
import { statusLabel } from './ui'
const fields: Record<string, Array<[string, string]>> = {
  bugfix: [['regression', '回归验证结论']],
  startup: [['startup_command', '最终启动命令'], ['health', '健康检查']],
  release: [['build_artifacts', '构建产物'], ['health', '健康检查'], ['rollback', '回滚步骤']],
  dependencies: [['regression', '兼容性回归验证']],
}
export default function OperationResults({ run }: { run: Run }) {
  const source = run.source as { operation?: string; type?: string } | undefined
  const entries = fields[source?.operation ?? '']
  if (!entries) return null
  const results = run.artifacts?.operation_results as Record<string, { status: string; evidence: string }> | undefined
  const targets = (run.source as { remote_targets?: Array<{ id: string; name: string }> } | undefined)?.remote_targets ?? []
  const remote = (run.artifacts?.remote_results ?? []) as Array<{ target_id: string; verb: string; status: string; exit_code: number | null; executed: boolean; reason?: string }>
  return <section className="wb-detail-card" aria-label="维护结果"><h2>{source?.type === 'inspection' ? '巡检结果 · 只诊断' : '维护结果'}</h2><p>来自本次验收证据；缺少证据的项目明确标记为未验证。</p>{entries.map(([key, label]) => <div className="wb-check-detail" key={key}><h3>{label} · {statusLabel(results?.[key]?.status ?? 'unverified')}</h3><p style={{ whiteSpace: 'pre-wrap' }}>{results?.[key]?.evidence || '尚无可引用的验收证据。'}</p></div>)}{(source?.operation === 'release' || (source?.type === 'inspection' && targets.length > 0)) && <div aria-label="服务器交付">{targets.length === 0 ? <p>未连接服务器，仅完成准备</p> : targets.map(target => <div key={target.id}><h3>{target.name}</h3>{(source?.type === 'inspection' ? ['health_check', 'service_status'] as const : ['health_check', 'deploy', 'rollback'] as const).map(verb => { const result = remote.filter(row => row.target_id === target.id && row.verb === verb).slice(-1)[0]; return <p key={verb}>{{ health_check: '健康检查', service_status: '服务状态', deploy: '部署', rollback: '回滚' }[verb]}：{result ? `${statusLabel(result.status)} · ${result.executed ? '已执行' : '未确认执行'} · 退出码 ${result.exit_code ?? '未知'} ${result.reason || ''}` : '未执行'}</p> })}</div>)}</div>}</section>
}
