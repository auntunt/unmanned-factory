import { matchPath } from 'react-router-dom'
import type { IconName } from '../workbench/Icon'

/** Single source of truth for global navigation, route ownership, titles,
 *  breadcrumbs and in-page tabs. The shell and every page read from here so
 *  there is never a second menu to maintain. */
export type NavKey = 'start' | 'history' | 'agents' | 'engineering' | 'settings'
export interface NavItem { key: NavKey; to: string; label: string; icon: IconName }

export const PRIMARY_NAV: NavItem[] = [
  { key: 'start', to: '/', label: '开始制作', icon: 'home' },
  { key: 'history', to: '/history', label: '历史作品', icon: 'history' },
  { key: 'agents', to: '/agents', label: '职能体', icon: 'agent' },
  { key: 'engineering', to: '/overview', label: '工程总览', icon: 'project' },
]
export const SETTINGS_NAV: NavItem = { key: 'settings', to: '/settings', label: '设置', icon: 'settings' }

export interface GroupTab { to: string; label: string; end?: boolean; adminOnly?: boolean }
export const GROUP_TABS: Record<'engineering' | 'agents' | 'settings', GroupTab[]> = {
  engineering: [
    { to: '/overview', label: '总览', end: true }, { to: '/projects', label: '项目' },
    { to: '/runs', label: '运行记录', end: true }, { to: '/costs', label: '用量与预算', adminOnly: true }, { to: '/team', label: '团队', adminOnly: true },
  ],
  agents: [{ to: '/agents', label: '职能体', end: true }, { to: '/ability-center', label: '能力库' }],
  settings: [{ to: '/settings', label: '通用', end: true }, { to: '/settings/runtime', label: '模型与执行', adminOnly: true }],
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
  /** breadcrumb trail; the last item is the current page. */
  breadcrumb: { label: string; to?: string }[]
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
  if (is('/agents/:agentId')) return { activeKey: 'agents', title: '职能体详情', group: 'agents', activeTab: '/agents', breadcrumb: [{ label: '职能体', to: '/agents' }, { label: '详情' }] }
  if (is('/ability-center') || is('/modules') || is('/capabilities')) return { activeKey: 'agents', title: '能力库', group: 'agents', activeTab: '/ability-center', breadcrumb: [{ label: '职能体', to: '/agents' }, { label: '能力库' }] }
  if (is('/overview')) return { activeKey: 'engineering', title: '工程总览', group: 'engineering', activeTab: '/overview', breadcrumb: [{ label: '工程总览' }] }
  if (is('/projects')) return { activeKey: 'engineering', title: '项目', group: 'engineering', activeTab: '/projects', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '项目' }] }
  if (is('/projects/:projectId')) return { activeKey: 'engineering', title: '项目详情', group: 'engineering', activeTab: '/projects', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '项目', to: '/projects' }, { label: '详情' }] }
  if (is('/runs')) return { activeKey: 'engineering', title: '运行记录', group: 'engineering', activeTab: '/runs', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '运行记录' }] }
  if (is('/costs')) return { activeKey: 'engineering', title: '用量与预算', group: 'engineering', activeTab: '/costs', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '用量与预算' }] }
  if (is('/team')) return { activeKey: 'engineering', title: '团队', group: 'engineering', activeTab: '/team', breadcrumb: [{ label: '工程总览', to: '/overview' }, { label: '团队' }] }
  if (is('/settings/runtime')) return { activeKey: 'settings', title: '模型与执行', group: 'settings', activeTab: '/settings/runtime', breadcrumb: [{ label: '设置', to: '/settings' }, { label: '模型与执行' }] }
  if (is('/settings')) return { activeKey: 'settings', title: '设置', group: 'settings', activeTab: '/settings', breadcrumb: [{ label: '设置' }] }
  return { activeKey: 'start', title: '开始制作', breadcrumb: [{ label: '开始制作' }] }
}
