// 权限门事件面板：显示一轮 attempt 里被拦/升级的动作。
// 用 ALLOW 不记录，所以这里只有 DENY 和 ESCALATE。

import type { PermissionEvent } from '../types'

interface PermissionPanelProps {
  events: PermissionEvent[]
}

const DECISION_STYLE: Record<string, string> = {
  deny: 'bg-rose-50 border-rose-300 text-rose-900',
  escalate: 'bg-amber-50 border-amber-300 text-amber-900',
}

const DECISION_LABEL: Record<string, string> = {
  deny: '拦截',
  escalate: '升级',
}

export default function PermissionPanel({ events }: PermissionPanelProps) {
  if (!events || events.length === 0) {
    return null
  }

  return (
    <section className="mt-4 space-y-2">
      <h3 className="font-mono text-xs font-medium text-slate-500 uppercase tracking-wide">
        权限门事件 ({events.length})
      </h3>
      <div className="space-y-2">
        {events.map((e, idx) => {
          const style = DECISION_STYLE[e.decision] || 'bg-slate-50 border-slate-300'
          const label = DECISION_LABEL[e.decision] || e.decision
          return (
            <div
              key={idx}
              className={`border rounded px-3 py-2 font-mono text-xs ${style}`}
            >
              <div className="flex items-baseline gap-2 mb-1">
                <span className="font-semibold">{label}</span>
                <span className="text-slate-600">{e.tool}</span>
                {e.tokens > 0 && (
                  <span className="ml-auto text-slate-500 text-[10px]">
                    {e.tokens} tok · ${e.cost_usd.toFixed(4)}
                  </span>
                )}
              </div>
              <div className="text-slate-700 mb-1 break-all">
                <span className="text-slate-500">目标:</span> {e.target}
              </div>
              <div className="text-slate-700 mb-1">
                <span className="text-slate-500">规则:</span> {e.rule}
              </div>
              <div className="text-slate-700">
                <span className="text-slate-500">原因:</span> {e.reason}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
