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
  return <section className="wb-detail-card" aria-label="维护结果"><h2>{source?.type === 'inspection' ? '巡检结果 · 只诊断' : '维护结果'}</h2><p>来自本次验收证据；缺少证据的项目明确标记为未验证。</p>{entries.map(([key, label]) => <div className="wb-check-detail" key={key}><h3>{label} · {statusLabel(results?.[key]?.status ?? 'unverified')}</h3><p style={{ whiteSpace: 'pre-wrap' }}>{results?.[key]?.evidence || '尚无可引用的验收证据。'}</p></div>)}</section>
}
