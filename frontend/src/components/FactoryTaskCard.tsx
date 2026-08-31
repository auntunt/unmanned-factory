import { Tag, Tooltip } from 'antd'
import { C, MONO } from '../theme/tokens'
import { ago, fmtUsd } from '../theme/geometry'

/**
 * 任务卡 —— 移植自 OA 的 TicketCard。
 *
 * 复刻件那张卡上「紧急程度」存的是整句中文，既当文案又当 SLA 判定 key
 * （复刻笔记明确标为反模式）。工厂里对应位置换成 oracle_class：
 * 一个枚举 A/B/C/D，展示文案与判定键分离。
 */

export interface TaskRow {
  task_id: string
  prompt_preview: string
  checks_count: number
  mtime: string
  resolution?: string
  attempts_count?: number
  total_cost_usd?: number
  oracle_class?: string
  class_reason?: string
  // 后端 analytics.stuck 用的是 stuck_days（不是 hours），别再换算一遍
  stuck_days?: number
}

// 预分级四类 —— 展示文案与判定键分离
const ORACLE_META: Record<string, { label: string; color: string; hint: string }> = {
  A: { label: 'A 全自动', color: 'green', hint: '机器可判、四性质齐备 → 可全自动' },
  B: { label: 'B 加审查', color: 'blue', hint: '可判但影响面大 → 自动执行 + 对抗 review' },
  C: { label: 'C 不无人', color: 'orange', hint: '无廉价裁判 → 不适合无人化' },
  D: { label: 'D 硬闸门', color: 'red', hint: '不可逆操作 → 只生成脚本，不执行' },
}

const RESOLUTION_META: Record<string, { label: string; color: string }> = {
  merged: { label: '已合并', color: 'success' },
  reworked: { label: '返工', color: 'magenta' },
  escalated: { label: '已升级', color: 'warning' },
  human_override: { label: '人工改判', color: 'purple' },
  pending: { label: '未定案', color: 'default' },
  'n/a': { label: '未派发', color: 'default' },
}

interface Props {
  task: TaskRow
  onClick?: (t: TaskRow) => void
}

export default function FactoryTaskCard({ task, onClick }: Props) {
  const oc = task.oracle_class ? ORACLE_META[task.oracle_class] : null
  const res = task.resolution ? RESOLUTION_META[task.resolution] : null
  const stuck = (task.stuck_days ?? 0) > 2
  // 兜底分级：grading/rules.py 匹配不到规则时给 default A。语义上是「没判据」，
  // 但值和「有强判据」的 A 一模一样，只能靠 class_reason 区分。
  const isDefaultClass = /no rule matched|default/i.test(task.class_reason ?? '')
  // 轮次深：复刻件教训 —— 实测有工单复核 10 次死循环，要在卡面上就能看见
  const deepRework = (task.attempts_count ?? 0) >= 5

  return (
    <div
      onClick={() => onClick?.(task)}
      style={{
        background: C.bgCard,
        border: `1px solid ${stuck ? C.errorBorder : C.borderLight}`,
        borderRadius: 8,
        padding: '10px 12px',
        cursor: 'pointer',
        marginBottom: 8,
        boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
      }}
    >
      <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
        {oc ? (
          // class_reason 是后端评级时写下的真实理由，比静态释义有用得多，有就优先显示。
          // 实测线上全部是 "no rule matched -> default A" —— 兜底值，不是真判定，
          // 直接显示成 A（最可信）会把最没判据的任务标成最可信的。isDefault 会降级显示。
          <Tooltip
            title={
              isDefaultClass
                ? `分级是兜底值，不是真判定：${task.class_reason}。oracle_rules.yaml 里没有能匹配这个任务的规则。`
                : task.class_reason || oc.hint
            }
          >
            <Tag
              bordered={false}
              color={isDefaultClass ? 'default' : oc.color}
              style={{ margin: 0, fontSize: 11 }}
            >
              {isDefaultClass ? `${task.oracle_class} · 兜底未判` : oc.label}
            </Tag>
          </Tooltip>
        ) : (
          <Tooltip title="还没跑过，没有预分级记录">
            <Tag bordered={false} style={{ margin: 0, fontSize: 11 }}>
              未分级
            </Tag>
          </Tooltip>
        )}
        {res && (
          <Tag bordered={false} color={res.color} style={{ margin: 0, fontSize: 11 }}>
            {res.label}
          </Tag>
        )}
        {stuck && (
          <Tag bordered={false} color="error" style={{ margin: 0, fontSize: 11 }}>
            滞留 {(task.stuck_days ?? 0).toFixed(1)} 天
          </Tag>
        )}
        {deepRework && (
          <Tooltip title="轮次偏深，注意返工死循环">
            <Tag bordered={false} color="magenta" style={{ margin: 0, fontSize: 11 }}>
              {task.attempts_count} 轮
            </Tag>
          </Tooltip>
        )}
        {task.checks_count === 0 && (
          <Tooltip title="没有可机器判定的验收判据，监工无从裁决">
            <Tag bordered={false} color="warning" style={{ margin: 0, fontSize: 11 }}>
              无判据
            </Tag>
          </Tooltip>
        )}
      </div>

      <div
        style={{
          fontSize: 12,
          color: C.textSub,
          fontFamily: MONO,
          marginBottom: 4,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {task.task_id}
      </div>

      <div style={{ fontSize: 13, color: C.text, lineHeight: 1.5, marginBottom: 6 }}>
        {task.prompt_preview}
      </div>

      <div
        style={{
          fontSize: 11,
          color: C.textSub,
          display: 'flex',
          justifyContent: 'space-between',
          lineHeight: 1.6,
        }}
      >
        <span style={{ fontFamily: MONO }}>
          {task.checks_count} 判据
          {task.total_cost_usd != null && ` · ${fmtUsd(task.total_cost_usd)}`}
        </span>
        <span>{ago(task.mtime)}</span>
      </div>
    </div>
  )
}
