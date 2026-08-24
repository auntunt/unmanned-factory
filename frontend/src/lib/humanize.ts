// 把内部枚举和原始遥测翻成操作者能读的人话。
//
// 诚实性要求（不是样式偏好）：所有查表都用 `?? 原值` 兜底。
// 遇到没登记过的 code，原样显示比静默丢掉好——操作者至少还能搜。

import type { Resolution, VerdictRole } from '../types'

/** 每轮的结局：一句话说清发生了什么 + 操作者该做什么。 */
interface ResolutionText {
  /** 结局本身，动词开头 */
  label: string
  /** 这轮之后系统做了什么 */
  what: string
  tone: 'ok' | 'wait' | 'stop'
}

export const RESOLUTION_TEXT: Record<Resolution | string, ResolutionText> = {
  merged: {
    label: '验收通过',
    what: '改动已经落到分支上，这个任务完成了。',
    tone: 'ok',
  },
  reworked: {
    label: '打回重做',
    what: '监工不认可，系统换更强的模型再试一轮。不用你管。',
    tone: 'wait',
  },
  escalated: {
    label: '交给人',
    what: '最强的模型也没过关，系统停手了，等你看一眼。',
    tone: 'stop',
  },
  blocked: {
    label: '不许自动做',
    what: '这类改动规定不能无人值守，得你自己跑。',
    tone: 'stop',
  },
  not_dispatched: {
    label: '没派工',
    what: '还没开始干就被风险监工拦下了，一分钱没花。',
    tone: 'stop',
  },
  pending: {
    label: '进行中',
    what: '这轮还在跑，结果没出来。',
    tone: 'wait',
  },
}

export function resolutionText(r: Resolution | string): ResolutionText {
  return (
    RESOLUTION_TEXT[r] ?? {
      label: r,
      what: '这是个没登记过的状态，去审计库查 resolution 字段。',
      tone: 'stop',
    }
  )
}

/** 模型 → 它在流水线里扮演的角色（便宜先试，贵的兜底）。 */
const MODEL_TEXT: Record<string, string> = {
  haiku: '最便宜的 AI 先试',
  sonnet: '换中档 AI 再试',
  opus: '上最强的 AI',
}

export function modelText(model: string): string {
  return MODEL_TEXT[model] ?? `用 ${model} 试`
}

/** 三个监工各管什么。给的是职责，不是名字。 */
export const ROLE_TEXT: Record<VerdictRole | string, { name: string; duty: string }> = {
  risk: { name: '风险监工', duty: '判这活能不能让机器无人值守地做' },
  regression: { name: '回归监工', duty: '跑测试，看有没有把原来好的东西弄坏' },
  scope: { name: '范围监工', duty: '看有没有改到没让它改的地方' },
  beacon: { name: '灯塔监工', duty: '抽查产出质量' },
}

export function roleText(role: VerdictRole | string) {
  return ROLE_TEXT[role] ?? { name: String(role), duty: '未登记的监工类型' }
}
