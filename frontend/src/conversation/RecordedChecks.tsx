import type { Run } from '../workspace/types'
import { checkPassed } from '../workbench/run-guidance'

type Record = { [key: string]: unknown }
export type RecordedCheck = { check: Record; context: string }
const isRecord = (value: unknown): value is Record => Boolean(value) && typeof value === 'object' && !Array.isArray(value)

/** Keep final integration results separate from historical repair attempts. */
export function recordedChecks(run: Run): RecordedCheck[] {
  const result: RecordedCheck[] = []
  const add = (value: unknown, context: string) => {
    if (Array.isArray(value)) for (const check of value) if (isRecord(check)) result.push({ check, context })
  }
  add(run.artifacts?.checks, '集成检查')
  const tasks = Array.isArray(run.artifacts?.tasks) ? run.artifacts.tasks : run.tasks
  if (Array.isArray(tasks)) for (const task of tasks) {
    if (!isRecord(task)) continue
    const label = typeof task.id === 'string' ? '任务 ' + task.id : '任务'
    if (Array.isArray(task.attempts) && task.attempts.length) {
      task.attempts.forEach((attempt, index) => {
        if (isRecord(attempt)) add(attempt.checks, label + ' · 第 ' + (typeof attempt.attempt === 'number' ? attempt.attempt : index + 1) + ' 次尝试')
      })
    } else add(task.checks, label)
  }
  return result
}

export default function RecordedChecks({ rows }: { rows: RecordedCheck[] }) {
  if (!rows.length) return null
  return <section className="cv-recorded-checks" aria-label="已记录的命令检查">
    <p className="cv-hint">以下为真实执行记录；历史尝试与最终集成检查分别展示，不代表完整需求验收。</p>
    {rows.map(({ check, context }, index) => {
      const name = typeof check.name === 'string' ? check.name : '检查 ' + (index + 1)
      const passed = checkPassed(check)
      const outcome = passed ? '通过' : check.timeout ? '超时' : check.cancelled ? '已取消' : typeof check.exit === 'number' ? '未通过' : '未验证'
      const argv = Array.isArray(check.argv) && check.argv.every(item => typeof item === 'string') ? check.argv as string[] : []
      return <details className="cv-recorded-check" key={context + ':' + index}>
        <summary><span>{context} · {name}</span><strong className={passed ? 'cv-verify-pass' : 'cv-verify-fail'}>{outcome}</strong></summary>
        <div className="ew-check-meta"><span>退出码：{typeof check.exit === 'number' ? check.exit : '未记录'}</span>{typeof check.duration_s === 'number' && <span>耗时：{check.duration_s}s</span>}</div>
        {argv.length > 0 && <pre aria-label="检查命令参数">{JSON.stringify(argv, null, 2)}</pre>}
        {typeof check.stdout === 'string' && <div><small>标准输出</small><pre>{check.stdout || '无输出'}</pre></div>}
        {typeof check.stderr === 'string' && <div><small>标准错误</small><pre>{check.stderr || '无输出'}</pre></div>}
      </details>
    })}
  </section>
}
