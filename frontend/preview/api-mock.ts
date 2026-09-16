// Preview-only transport: returns fixtures so the REAL conversation components
// render each run state without a backend. Never used in the production build.
export class WorkspaceApiError extends Error {
  status: number; detail: string
  constructor(status: number, detail: string) { super(detail); this.status = status; this.detail = detail }
}
const now = new Date().toISOString()
const RUNS: Record<string, Record<string, unknown>> = {
  active: { id: 'active', project_id: 'p1', request: '做一个预约管理工具，支持新增、改期和导出。', revision: 1,
    status: 'running', plan: { title: '预约管理工具', summary: '', questions: [], tasks: [] }, artifacts: {}, created_at: now, updated_at: now },
  recover: { id: 'recover', project_id: 'p1', request: '按我上传的表格导入客户名单。', revision: 1,
    status: 'needs_human', plan: { title: '预约管理工具', summary: '', questions: [], tasks: [] },
    artifacts: { failure_reason: '上传的表格缺少「预约时间」这一列，暂时无法完成导入。已有成果已保留。' }, created_at: now, updated_at: now },
  delivered: { id: 'delivered', project_id: 'p1', request: '帮我做一个预约管理工具，能新增预约、修改时间并导出记录。', revision: 2,
    status: 'ready_for_review', plan: { title: '预约管理工具', summary: '', questions: [], tasks: [] },
    artifacts: { commit: 'abc123', acceptance_ledger: { total: 3, counts: { pass: 3, fail: 0, unverified: 0 },
      items: [ { id: 'a', text: '新增预约后出现在列表中', status: 'pass' }, { id: 'b', text: '修改时间并保存成功', status: 'pass' }, { id: 'c', text: '导出记录为 CSV', status: 'pass' } ] } }, created_at: now, updated_at: now },
}
const MSGS: Record<string, unknown[]> = {
  active: [ { id: 1, role: 'user', content: '做一个预约管理工具，支持新增、改期和导出。', at: now },
            { id: 2, role: 'assistant', content: '页面结构已完成，正在接入预约操作。', at: now } ],
  recover: [ { id: 1, role: 'user', content: '按我上传的表格导入客户名单。', at: now } ],
  delivered: [ { id: 1, role: 'user', content: '帮我做一个预约管理工具，能新增预约、修改时间并导出记录。', at: now } ],
}
const DELIVERABLES: Record<string, unknown> = { items: [
  { id: 'p1', name: 'preview', kind: 'web', preview: true },
  { id: 'f1', name: '预约管理源码.zip', kind: 'source', size: 12400000 } ], recommended_preview_id: 'p1', can_collect: true }

const RUN_LIST = [
  { id: 'delivered', project_id: 'p1', request: '帮我做一个预约管理工具，能新增预约、修改时间并导出记录。', status: 'ready_for_review', revision: 2, plan: { title: '预约管理工具' }, updated_at: now },
  { id: 'active', project_id: 'p1', request: '做一个客户订单管理应用，可以录入、搜索和导出订单。', status: 'running', revision: 1, plan: { title: '订单管理应用' }, updated_at: now },
  { id: 'recover', project_id: 'p1', request: '按我上传的表格导入客户名单。', status: 'needs_human', revision: 1, plan: { title: '客户名单导入' }, updated_at: now },
]
const AGENTS = [
  { id: 'a1', name: '逆向分析助手', purpose: '用于理解现有代码与程序结构、私有格式转换。', active_version: 3, builtin_pack: undefined },
  { id: 'a2', name: '老项目渐进改造', purpose: '维护已有系统、分步做技术改造，全程可回退。', active_version: 1, builtin_pack: 'legacy-modernization' },
  { id: 'a3', name: '项目代码梳理', purpose: '理解陌生项目、做代码交接，事实与推断分开。', active_version: 1, builtin_pack: 'codebase-map' },
  { id: 'a4', name: '自动化 CLI 构建', purpose: '把重复操作做成可安装、可脚本调用的命令行工具。', active_version: 1, builtin_pack: 'automation-cli' },
  { id: 'a5', name: '需求分析', purpose: '把一句话需求整理成规格草案与可验收样例。', active_version: 1, builtin_pack: 'requirements' },
]

export async function request<T>(path: string): Promise<T> {
  const run = (id: string) => RUNS[id] || RUNS.active
  const m = path.match(/\/api\/v2\/runs\/([^/?]+)(\/conversation)?$/)
  if (m && m[2]) return { messages: MSGS[m[1]] || [] } as T
  if (m) return { ...run(m[1]), progress: {} } as T
  if (path === '/api/v2/runs') return { runs: RUN_LIST } as T
  if (path === '/api/v4/agents') return { agents: AGENTS } as T
  if (path === '/api/v3/environment') return { mode: 'preview', label: '演练' } as T
  if (path.endsWith('/deliverables')) return DELIVERABLES as T
  if (path === '/api/auth/me') return { user: { id: 1, username: 'owner', role: 'admin' }, csrf_token: 'x' } as T
  return {} as T
}
