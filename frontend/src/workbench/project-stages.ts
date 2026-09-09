export const PROJECT_STAGES = [
  { id: 'intake', label: '需求澄清', view: 'requirements', action: '查看需求与答复', empty: '从这个项目的一条需求开始', description: '原始需求、补充问题和已确认的目标。' },
  { id: 'plan', label: '方案规划', view: 'plan', action: '查看方案与分工', empty: '需求明确后，在这里形成方案', description: '实施方案、任务依赖和验收约定。' },
  { id: 'build', label: '开发执行', view: 'execution', action: '查看执行现场', empty: '方案进入执行后，在这里跟踪开发', description: '实际任务、模型分工、尝试和修复记录。' },
  { id: 'verify', label: '质量验证', view: 'verification', action: '查看检查结果', empty: '执行产生检查结果后，在这里核验', description: '已运行的检查、失败证据和验证结果。' },
  { id: 'deliver', label: '交付发布', view: 'delivery', action: '查看交付产物', empty: '生成交付产物后，在这里复核和发布', description: '代码提交、交付分支、发布状态和 PR。' },
  { id: 'reuse', label: '能力沉淀（可选）', view: null, action: '查看能力与来源', empty: '有值得复用的经验时，再沉淀能力', description: '按需提炼可复用经验，不影响任务完成或成果领取。' },
] as const

export type ProjectStageId = typeof PROJECT_STAGES[number]['id']

export function projectStage(value: string | null | undefined) {
  return PROJECT_STAGES.find((item) => item.id === value) ?? PROJECT_STAGES[0]
}

export function projectStageHref(projectId: string | number, stage: string) {
  return `/projects/${encodeURIComponent(String(projectId))}?stage=${encodeURIComponent(stage)}`
}
