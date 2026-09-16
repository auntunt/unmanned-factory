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
const DELIVERABLES: Record<string, unknown> = { saved: true, items: [
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

const PROJECTS = [{ id: 'p1', name: '预约管理工具（示例）', repository: 'preview/appointments', workspace: '/preview/appointments', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false, revision: 1, agent_id: 'a3' }]
const ENGINEERING = { stages: [], verified_runs: 1, published_runs: 0, distilled_runs: 0, reused_runs: 0, draft_capabilities: 0, ready_capabilities: 0 }
const OVERVIEW = { snapshot_at: now, projects: 1, runs: 3, active_runs: 1, attention_runs: 1, delivered_runs: 1,
  known_cost_usd: 0, unknown_cost_runs: 3, model_usage: [], activity: [], capabilities: 0, recent_events: [],
  run_snapshots: RUN_LIST, engineering: ENGINEERING, project_summaries: PROJECTS.map(p => ({ ...p, run_count: 3, active_runs: 1, attention_runs: 1, engineering: ENGINEERING })),
  attention: [{ ...RUNS.recover, title: '客户名单导入', reason: '示例：需要补充预约时间列' }] }
const QUOTA = { limit_tokens: null, used_tokens: 0, reserved_tokens: 0, unknown_calls: 0, remaining_tokens: null }

export async function request<T>(path: string, options: { method?: string; signal?: AbortSignal; [key: string]: unknown } = {}): Promise<T> {
  if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
  if (options.method && !['GET', 'HEAD'].includes(options.method)) throw new WorkspaceApiError(405, '这是只读界面预览，未连接执行服务；本次操作没有保存或执行。')
  const url = new URL(path, 'http://preview.local')
  const pathname = url.pathname
  if (pathname === '/api/v2/projects') return { projects: PROJECTS } as T
  if (pathname === '/api/v3/overview') return OVERVIEW as T
  if (pathname === '/api/v3/team') return { month: now.slice(0, 7), timezone: 'Asia/Shanghai', reservation_tokens: 1000, workspace: QUOTA,
    members: [{ id: 1, username: 'owner', role: 'admin', active: true, project_ids: ['p1'], quota: QUOTA }],
    projects: [{ id: 'p1', name: PROJECTS[0].name, member_ids: [1], quota: QUOTA }], calls: [], audit: [] } as T
  if (pathname === '/api/v2/runtime/operations') return { revision: 0, webhook_configured: false, knowledge_enabled: false } as T
  if (pathname === '/api/v2/deploy-targets') return { targets: [] } as T
  if (pathname === '/api/v2/runtime') return { revision: 0, profiles: {}, tools: [], blockers: ['只读预览，未连接执行服务'], last_probes: [], updated_at: null } as T
  if (pathname === '/api/v3/capabilities') return { capabilities: [] } as T
  if (pathname.endsWith('/abilities/preflight')) return { ready: false, message: '只读预览：能力加载需在真实工作台进行。' } as T
  if (/^\/api\/v4\/agents\/[^/]+\/skills$/.test(pathname)) return { skills: [] } as T
  if (pathname === '/api/v4/skill-ingestions') return { items: [] } as T
  if (pathname === '/api/v4/modules') return { modules: [] } as T
  if (pathname === '/api/v2/project-candidates') return { candidates: [], root_available: false } as T
  if (pathname === '/api/v2/operation-presets') return { presets: [] } as T
  if (pathname.endsWith('/readiness')) return { project_id: 'p1', ready: false, checks: [{ id: 'preview', label: '预览环境', status: 'warning', message: '未连接真实执行服务' }] } as T
  const agentMatch = pathname.match(/^\/api\/v4\/agents\/([^/]+)(\/conversations|\/versions|\/evolution|\/draft|\/manifest)?$/)
  if (agentMatch) {
    const agent = AGENTS.find(a => a.id === agentMatch[1])
    if (!agent) throw new WorkspaceApiError(404, '示例职能体不存在')
    const version = { version: agent.active_version, instructions: agent.purpose, tool_scope: [], acceptance: [], skill_ids: [], model_settings: {} }
    if (agentMatch[2] === '/draft') return { agent_id: agent.id, base_version: agent.active_version, revision: 0, patch: {} } as T
    if (agentMatch[2] === '/manifest') return { revision: 1, identity: agent.purpose, skills: [], assertions: [], history: [] } as T
    if (agentMatch[2] === '/conversations') return { conversations: [] } as T
    if (agentMatch[2] === '/versions') return { versions: [version] } as T
    if (agentMatch[2] === '/evolution') return { proposals: [] } as T
    return { ...agent, version } as T
  }
  const run = (id: string) => { if (!RUNS[id]) throw new WorkspaceApiError(404, '预览中没有这条任务'); return RUNS[id] }
  const m = path.match(/\/api\/v2\/runs\/([^/?]+)(\/conversation)?$/)
  if (m && m[2]) return { messages: MSGS[m[1]] || [] } as T
  if (m) return { ...run(m[1]), progress: {} } as T
  if (path === '/api/v2/runs') return { runs: RUN_LIST } as T
  if (path === '/api/v4/agents') return { agents: AGENTS } as T
  if (path === '/api/v3/environment') return { mode: 'preview', label: '演练' } as T
  if (pathname.endsWith('/deliverables/files/p1') && url.searchParams.get('preview') === 'true') return { name: '预约管理工具（示例）', kind: 'web', content: '<main style="padding:32px;font-family:system-ui;background:#fafaf7;color:#202b2a"><h1>预约管理工具</h1><p>只读界面样例</p><table><tr><th>客户</th><th>预约时间</th></tr><tr><td>示例客户</td><td>周三 10:00</td></tr></table></main>' } as T
  if (path.endsWith('/deliverables')) return DELIVERABLES as T
  if (path === '/api/auth/me') return { user: { id: 1, username: 'owner', role: 'admin' }, csrf_token: 'x' } as T
  throw new WorkspaceApiError(501, `该数据尚无预览样例：${pathname}。请在真实服务中查看。`)
}
