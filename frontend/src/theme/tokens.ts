/**
 * 设计 token —— 来自 OA 闭环监控页的实测采集（getComputedStyle，2026-08-26）
 *
 * 原样保留而不是换成工厂旧的 tailwind 配色：这套值是从一个真实在生产环境
 * 用了很久的看板上量出来的，色阶关系（main/light/border/bg 四档）已经被
 * 实际使用检验过。改配色是另一件事，不该和这次移植混在一起做。
 */

export const C = {
  primary: '#1677FF',
  primaryDark: '#0958D9',
  success: '#52C41A',
  successDark: '#389E0D',
  successBorder: '#B7EB8F',
  successBg: '#F6FFED',
  warning: '#FAAD14',
  error: '#FF4D4F',
  errorDark: '#F5222D',
  errorBorder: '#FFCCC7',
  errorBg: '#FFF2F0',
  text: '#262626',
  textSub: '#8C8C8C',
  textDisabled: '#BFBFBF',
  border: '#D9D9D9',
  borderLight: '#F0F0F0',
  bgCard: '#FFFFFF',
  bgHead: '#FAFAFA',
  bgPage: '#F1F5F9',
  slate: '#CBD5E1',
  slateDark: '#64748B',
} as const

/**
 * 七个闭环节点的语义配色。
 *
 * 节点键换成了工厂的概念（inbox/running/judging/…），但色相序列沿用
 * 复刻件的：灰起点 → 蓝进行 → 紫审查 → 粉验证 → 青就绪 → 橙等人 → 绿终态。
 * 这个序列本身携带信息（冷色=机器在跑，暖色=要人介入，绿=完成）。
 */
export const NODE_COLORS: Record<string, { main: string; light: string; label: string }> = {
  inbox:      { main: '#64748B', light: '#94A3B8', label: '石板灰·中性起点' },
  running:    { main: '#1677FF', light: '#4096FF', label: '蓝·进行中' },
  judging:    { main: '#722ED1', light: '#9254DE', label: '紫·监工裁决' },
  reworking:  { main: '#EB2F96', light: '#F759AB', label: '粉·返工' },
  done:       { main: '#13C2C2', light: '#36CFC9', label: '青·已定案' },
  needs_human:{ main: '#FA8C16', light: '#FFA940', label: '橙·等人介入' },
  merged:     { main: '#52C41A', light: '#73D13D', label: '绿·终态成功' },
  blocked:    { main: '#FF4D4F', light: '#FF7875', label: '红·终态失败' },
  parked:     { main: '#8C8C8C', light: '#BFBFBF', label: '灰·搁置' },
}

// 环形几何 —— 实测：圆心(826,690) 半径204 节点直径68
export const RING = {
  size: 530,
  radius: 204,
  nodeSize: 68,
  centerSize: 234,
  startAngle: -90,
} as const

export const FONT =
  '-apple-system, system-ui, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'

// 等宽字体：任务 id、commit、金额、token 数都该对齐
export const MONO =
  'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace'
