import { useEffect, useRef, useState } from 'react'
import { request, WorkspaceApiError } from '../workspace/api'
import type { Plan, Run } from '../workspace/types'
import { ErrorNotice, formatDate, errorText } from './ui'
import './run-guidance.css'

export interface PlanVersion { revision: number; summary: string; plan?: Plan; at?: string }

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function validPlan(value: unknown): Plan | undefined {
  if (!record(value) || typeof value.title !== 'string' || typeof value.summary !== 'string' || !Array.isArray(value.tasks) || !Array.isArray(value.questions)) return undefined
  return value as unknown as Plan
}

/** Keeps the current run snapshot visible even when no historical record is available. */
export function planVersions(run: Run, saved: PlanVersion[]): PlanVersion[] {
  const byRevision = new Map<number, PlanVersion>()
  for (const version of saved) {
    if (!Number.isFinite(version.revision)) continue
    const plan = validPlan(version.plan)
    byRevision.set(version.revision, {
      revision: version.revision,
      summary: typeof version.summary === 'string' ? version.summary : plan?.summary ?? '未记录摘要',
      plan,
      at: typeof version.at === 'string' ? version.at : undefined,
    })
  }
  if (run.plan && !byRevision.has(run.revision)) byRevision.set(run.revision, { revision: run.revision, summary: run.plan.summary, plan: run.plan, at: run.updated_at })
  return [...byRevision.values()].sort((a, b) => b.revision - a.revision)
}

export default function PlanVersions({ run, onUnauthorized }: { run: Run; onUnauthorized: () => void }) {
  const [open, setOpen] = useState(false)
  const [saved, setSaved] = useState<PlanVersion[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  useEffect(() => { setOpen(false); setSaved(null); setError(null); controller.current?.abort() }, [run.id, run.revision])
  const load = async () => {
    if (saved !== null) return
    controller.current?.abort()
    const signalController = new AbortController()
    controller.current = signalController
    setError(null)
    try {
      const response = await request<{ versions?: unknown }>(`/api/v3/runs/${encodeURIComponent(String(run.id))}/plans`, { onUnauthorized, signal: signalController.signal })
      if (!signalController.signal.aborted) setSaved(Array.isArray(response.versions) ? response.versions.filter(record).map((item) => ({
        revision: typeof item.revision === 'number' ? item.revision : Number.NaN,
        summary: typeof item.summary === 'string' ? item.summary : '',
        plan: validPlan(item.plan),
        at: typeof item.at === 'string' ? item.at : undefined,
      })) : [])
    } catch (cause) {
      if (!signalController.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause))
    }
  }
  const toggle = (next: boolean) => { setOpen(next); if (next) void load() }
  const versions = planVersions(run, saved ?? [])
  return <details className="wb-plan-versions" open={open} onToggle={(event) => toggle((event.currentTarget as HTMLDetailsElement).open)}>
    <summary>计划版本记录（当前 v{run.revision}）</summary>
    <p>展开后按需读取最近保存的计划版本（最多 100 个）；可查看历史范围和验收，当前没有一键回滚功能。</p>
    {error && <ErrorNotice message={error} />}
    {open && saved === null && !error && <div className="wb-loading" role="status">正在读取计划版本记录…</div>}
    {versions.map((version) => <details className="wb-plan-version" key={version.revision} open={version.revision === run.revision}><summary><strong>v{version.revision}{version.revision === run.revision ? ' · 当前版本' : ''}</strong><span>{formatDate(version.at)}</span></summary><p>{version.summary}</p>{version.plan?.tasks.length ? <ul>{version.plan.tasks.map((task) => <li key={task.id}><strong>{task.title}</strong><span>范围：{task.paths.join('、') || '未记录'}；验收：{task.acceptance.join('；') || '未记录'}</span></li>)}</ul> : <p>该版本未保留完整任务明细。</p>}</details>)}
  </details>
}
