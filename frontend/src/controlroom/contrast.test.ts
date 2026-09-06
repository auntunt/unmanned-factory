/**
 * 控制室配色的对比度回归。
 *
 * 为什么要测 CSS：控制室是投屏给隔着桌子的人看的，浅色主题下
 * 「弱信息」色很容易被顺手调浅一档，肉眼当场看不出问题，
 * 会议室灯光下就糊了。这里把 WCAG AA 的数字钉死在测试里，
 * 谁改淡了谁的 CI 红。
 *
 * 值必须和 controlroom.css 的 :root 保持一致 —— 改 CSS 就要改这里，
 * 这份重复是故意的：让「改色」这个动作必须经过一次对比度确认。
 */
import { describe, expect, it } from 'vitest'

const AA = 4.5

/** 页面上真实存在的底色（半透明块已压平成实色） */
const BG = '#f4f6f8' // --bg 页底
const PANEL = '#ffffff' // --panel 卡片
const DIM_BG = '#e9eaea' // .pill.dim 的 rgba(15,19,24,.09) 压在白面板上
const STEP_BG = '#f3f3f3' // .cr-steps .s 的 rgba(15,19,24,.05) 压在白面板上

const INK = '#14181d'
const INK_2 = '#545c66'
const INK_3 = '#626a75'
const ICE = '#0f6fbf'
const PASS = '#1c7a3c'
const FAIL = '#bf2f2a'
const HOLD = '#9a6410'
const WHITE = '#ffffff'

function luminance(hex: string): number {
  const h = hex.replace('#', '')
  const ch = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
  const lin = ch.map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)))
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
}

function contrast(fg: string, bg: string): number {
  const a = luminance(fg)
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

describe('控制室浅色主题对比度', () => {
  it.each([
    ['主文字 on 页底', INK, BG],
    ['次文字 on 页底', INK_2, BG],
    ['弱文字 on 页底（空闲工位）', INK_3, BG],
    ['弱文字 on 白面板', INK_3, PANEL],
    ['「已关」标签字 on dim 底', INK_3, DIM_BG],
    ['阶段条未到达格字', INK_3, STEP_BG],
    ['ice on 白面板', ICE, PANEL],
    ['pass on 白面板', PASS, PANEL],
    ['fail on 白面板', FAIL, PANEL],
    ['hold on 白面板', HOLD, PANEL],
    ['now 格白字 on ice 实底', WHITE, ICE],
    ['stop 格白字 on hold 实底', WHITE, HOLD],
  ])('%s 过 AA', (_name, fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(AA)
  })

  it('弱文字仍明显弱于次文字，层级没塌', () => {
    // 弱信息要够看得见，但不能强到和次要文字打平，否则层级失效
    expect(contrast(INK_3, PANEL)).toBeLessThan(contrast(INK_2, PANEL) - 0.5)
  })

  it('阶段条当前格与已完成格靠明度差区分，不只靠色相', () => {
    // done 是 10% pass 淡底，now 是 ice 实底；色盲用户也要能分出「走到哪了」
    const doneBg = '#e8f2ec'
    expect(contrast(doneBg, ICE)).toBeGreaterThanOrEqual(3)
  })
})
