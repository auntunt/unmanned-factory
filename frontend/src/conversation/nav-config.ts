import { matchPath } from 'react-router-dom'
import type { IconName } from '../workbench/Icon'

/** Single source of truth for global navigation, route ownership, titles,
 *  breadcrumbs and in-page tabs. The shell and every page read from here so
 *  there is never a second menu to maintain. */
export type NavKey = 'start' | 'history' | 'agents' | 'engineering' | 'settings' | 'modernization' | 'maintenance' | 'adaptation' | 'management'
export interface NavItem { key: NavKey; to: string; label: string; icon: IconName }

export const PRIMARY_NAV: NavItem[] = [
  { key: 'start', to: '/', label: '开始制作', icon: 'home' },
  { key: 'history', to: '/history', label: '历史作品', icon: 'history' },
  { key: 'agents', to: '/agents', label: '职能体', icon: 'agent' },
  { key: 'engineering', to: '/overview', label: '工程总览', icon: 'project' },
]
// Business workspaces stay reachable when disabled so existing history remains accessible.
// Creation/continuation permissions remain enforced by each plugin's server-side gate.
export const BUSINESS_PLUGIN_NAV: NavItem[] = [
  { key: 'modernization', to: '/modernization', label: '信创化改造', icon: 'modules' },
  { key: 'maintenance', to: '/maintenance', label: '运维维护', icon: 'runs' },
  { key: 'adaptation', to: '/adaptation', label: '接口适配', icon: 'workflow' },
]
export const SETTINGS_NAV: NavItem = { key: 'settings', to: '/settings', label: '设置', icon: 'settings' }

export interface GroupTab { to: string; label: string; end?: boolean; adminOnly?: boolean }
export const GROUP_TABS: Record<'engineering' | 'agents' | 'settings', GroupTab[]> = {
  engineering: [
    { to: '/overview', label: '总览', end: true }, { to: '/projects', label: '项目' },
    { to: '/runs', label: '运行记录', end: true }, { to: '/costs', label: '用量与预算', adminOnly: true }, { to: '/team', label: '团队', adminOnly: true },
  ],
  // 能力围绕某个职能体管理，不再有并列的「能力库」页签。旧能力页面仍可直达，
  // 但作为无主导航的兼容次级页面，归属在「职能体」下。
  agents: [{ to: '/agents', label: '职能体', end: true }],
  settings: [{ to: '/settings', label: '通用', end: true }, { to: '/settings/runtime', label: '模型与执行', adminOnly: true }, { to: '/settings/plugins', label: '业务插件' }],
}

export type Group = 'engineering' | 'agents' | 'settings'
export interface Resolved {
  activeKey: NavKey
  title: string
  /** true when the top-bar title should come from the live work title (a task). */
  usesWorkTitle?: boolean
  group?: Group
  /** the `to` of the active in-page group tab, so detail routes still light their parent tab. */
  activeTab?: string
  /** whether the group's in-page tab strip should show (hidden on focused detail/chat views). */
  showTabs?: boolean
  /** breadcrumb trail; the last item is the current page.
   *  `live: true` means the label comes from the live work title (e.g. the
   *  agent's own name), which nav-config cannot know from the path alone. */
  breadcrumb: { label: string; to?: string; live?: boolean }[]
}

/** Ordered, explicit matching — never startsWith. `/runs/:id` is a task (history);
 *  `/runs` exactly is the engineering run list. They must not share a selection. */
export function resolveRoute(pathname: string): Resolved {
  const is = (pattern: string, end = true) => Boolean(matchPath({ path: pattern, end }, pathname))
  if (is('/')) return { activeKey: 'start', title: '开始制作', breadcrumb: [{ label: '开始制作' }] }
  if (is('/history')) return { activeKey: 'history', title: '历史作品', breadcrumb: [{ label: '历史作品' }] }
  if (is('/runs/:runId')) return { activeKey: 'history', title: '任务', usesWorkTitle: true, breadcrumb: [{ label: '历史作品', to: '/history' }, { label: '任务' }] }
  if (is('/agents')) return { activeKey: 'agents', title: '职能体', group: 'agents', activeTab: '/agents', breadcrumb: [{ label: '职能体' }] }
  if (is('/agents/:agentId/chat')) return { activeKey: 'agents', title: '对话', usesWorkTitle: true, group: 'agents', activeTab: '/agents', showTabs: false, breadcrumb: [{ label: '职能体', to: '/agents' }, { label: '对话' }] }
  if (is('/agents/:agentId')) return { activeKey: 'agents', title: '管理职能体', group: 'agents', activeTab: '/agents', breadcrumb: [{ label: '职能体', to: '/agents' }, { label: '职能体', live: true }, { label: '管理' }] }
  // 旧能力链接保留可用。它们没有自己的顶层页签了，选中态回到「职能体」，
  // 面包屑也回到职能体目录——不重定向，查询参数与来源上下文原样保留。
  if (is('/ability-center/packs/:packId')) return { activeKey: 'agents', title: '职能包详情', group: 'agents', activeTab: '/agents', showTabs: false, breadcrumb: [{ label: '职能体', to: '/agents' }, { label: '职能包' }] }
  if (is('/ability-center') || is('/modules') || is('/capabilities')) return { activeKey: 'agents', title: '能力资产', group: 'agents', activeTab: '/agents', showTabs: false, breadcrumb: [{ label: '职能体', to: '/agents' }, { label: '能力资产' }] }
  // 运维维护子系统有自己的三级入口；顶栏标题跟随子页面，而不是一律「详情」。
  if (is('/maintenance', false)) {
    const sub: [string, string][] = [['/maintenance/repos', '维护代码库'], ['/maintenance/intake', '需求接入'],
      ['/maintenance/tasks', '全部任务'], ['/maintenance/about', '技术详情']]
    const hit = sub.find(([path]) => is(path, false))
    const isTask = !hit && is('/maintenance/:taskId')
    const label = hit ? hit[1] : isTask ? '任务现场' : '运维监控'
    return {
      activeKey: 'maintenance', title: label, usesWorkTitle: isTask,
      breadcrumb: [{ label: '业务插件' }, { label: '运维维护', to: '/maintenance' }, { label }],
    }
  }
  for (const plugin of BUSINESS_PLUGIN_NAV) {
    const detail = is(plugin.to + '/:id')
    if (is(plugin.to) || detail) return {
      activeKey: plugin.key, title: plugin.label + (detail ? '详情' : ''),
      usesWorkTitle: detail,
      breadcrumb: [
        { label: '业务插件' },
        { label: plugin.label, ...(detail ? { to: plugin.to } : {}) },
        ...(detail ? [{ label: '详情' }] : []),
      ],
    }
  }
  // 管理面（企业治理 v1）：与工作面分开，不落回「开始制作」。
  if (is('/management')) return { activeKey: 'management', title: '管理首页', breadcrumb: [{ label: '管理' }] }
  if (is('/management/org')) return { activeKey: 'management', title: '组织与授权', breadcrumb: [{ label: '管理', to: '/management' }, { label: '组织与授权' }] }
  if (is('/management/projects/:projectId')) return { activeKey: 'management', title: '项目摘要', breadcrumb: [{ label: '管理', to: '/management' }, { label: '项目摘要' }] }
  if (is('/overview')) return { activeKey: 'engineering', title: '工程总览', group: 'engineering', activeTab: '/overview', breadcrumb: [{ label: '工程总览' }] }
  if (is('/projects')) return { activeKey: 'engineering', title: '项目', group: 'engineering', activeTab: '/projects', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '项目' }] }
  if (is('/projects/:projectId')) return { activeKey: 'engineering', title: '项目详情', group: 'engineering', activeTab: '/projects', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '项目', to: '/projects' }, { label: '详情' }] }
  if (is('/runs')) return { activeKey: 'engineering', title: '运行记录', group: 'engineering', activeTab: '/runs', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '运行记录' }] }
  if (is('/costs')) return { activeKey: 'engineering', title: '用量与预算', group: 'engineering', activeTab: '/costs', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '用量与预算' }] }
  if (is('/team')) return { activeKey: 'engineering', title: '团队', group: 'engineering', activeTab: '/team', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '团队' }] }
  if (is('/settings/runtime')) return { activeKey: 'settings', title: '模型与执行', group: 'settings', activeTab: '/settings/runtime', breadcrumb: [{ label: '设置', to: '/settings' }, { label: '模型与执行' }] }
  if (is('/settings/plugins')) return { activeKey: 'settings', title: '业务插件', group: 'settings', activeTab: '/settings/plugins', breadcrumb: [{ label: '设置', to: '/settings' }, { label: '业务插件' }] }
  if (is('/settings')) return { activeKey: 'settings', title: '设置', group: 'settings', activeTab: '/settings', breadcrumb: [{ label: '设置' }] }
  return { activeKey: 'start', title: '开始制作', breadcrumb: [{ label: '开始制作' }] }
}
