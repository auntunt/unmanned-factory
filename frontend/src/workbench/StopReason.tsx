/**
 * 展示运行「为什么停下来」。
 *
 * 后端在 artifacts 里写得很清楚（execution.py 的 stop_reason → artifacts.needs_human /
 * artifacts.billing_incomplete），但此前没有任何界面读它，用户只能看到状态变红、
 * 看不到原因，也不知道自己该做什么。这个组件把原因和下一步动作一起端出来。
 */
import type { Run } from '../workspace/types'

/** 预算类停止的原因文本形如 "observed provider cost $3.9453 exceeds budget $3.0000"。 */
const BUDGET_PATTERN = /cost \$([0-9.]+) exceeds budget \$([0-9.]+)/

function readReason(run: Run): { text: string; kind: 'budget' | 'billing' | 'other' } | null {
  const artifacts = run.artifacts
  if (!artifacts) return null
  const billing = artifacts.billing_incomplete
  if (typeof billing === 'string' && billing.trim()) return { text: billing, kind: 'billing' }
  const stop = artifacts.needs_human
  if (typeof stop !== 'string' || !stop.trim()) return null
  return { text: stop, kind: BUDGET_PATTERN.test(stop) ? 'budget' : 'other' }
}

/** 把英文原因翻成可执行的中文说明，同时保留原文供排查。 */
function explain(reason: { text: string; kind: string }): { title: string; detail: string; next: string[] } {
  const budget = reason.text.match(BUDGET_PATTERN)
  if (budget) {
    const [, spent, limit] = budget
    return {
      title: '已达预算上限，主动停止',
      detail: `本次运行实际花费 $${spent}，超过工程配置的预算上限 $${limit}。剩余任务没有开工，也不会自动发布。`,
      next: [
        `到工程设置里把预算调高到高于 $${spent} 的值，然后重新提交需求`,
        '或者把运行档位换成更便宜的模型后重跑',
        '不想继续就直接取消这次运行，已产出的分支和提交仍然保留',
      ],
    }
  }
  if (reason.kind === 'billing') {
    return {
      title: '用量数据不完整，等人确认',
      detail: reason.text,
      next: ['核对服务商侧的实际计费，确认可接受后再决定是否继续或发布'],
    }
  }
  return {
    title: '运行已停下，等人介入',
    detail: reason.text,
    next: ['按上面的原因处理后，用下方补充信息重新规划，或取消本次运行'],
  }
}

export function StopReason({ run }: { run: Run }) {
  const reason = readReason(run)
  if (!reason) return null
  const { title, detail, next } = explain(reason)
  const questions = run.triage?.questions ?? []
  return (
    <section className="wb-detail-card wb-stop-reason" role="alert" aria-labelledby="stop-reason-title">
      <div className="wb-detail-kicker">停止原因</div>
      <h2 id="stop-reason-title">{title}</h2>
      <p>{detail}</p>
      {reason.kind === 'budget' && questions.length === 0 && (
        <p className="wb-stop-reason-note">
          系统没有需要你回答的问题；这是预算上限触发的策略性停止。
        </p>
      )}
      <div className="wb-stop-reason-next">
        <strong>可以怎么做</strong>
        <ul>{next.map((item) => <li key={item}>{item}</li>)}</ul>
      </div>
      <details className="wb-stop-reason-raw">
        <summary>后端原始信息</summary>
        <code>{reason.text}</code>
      </details>
    </section>
  )
}
