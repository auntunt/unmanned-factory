// k9s 式的外框：顶部键值状态栏 + 底部快捷键栏。
// 浅色主题，但保留终端的信息密度和配色分工（标签淡、值重、状态用色）。

import type { ReactNode } from 'react'

/** 顶部状态栏的一格。label 淡色，value 等宽加重。 */
export interface StatItem {
  label: string
  value: ReactNode
  /** 值的额外配色，缺省是深色 */
  tone?: string
}

/**
 * k9s 顶部那条系统信息栏：两列键值密排，label 右对齐冒号，value 等宽。
 * 不用卡片、不留大间距——这里的目的是一眼扫完，不是好看。
 */
export function StatusBar({ items }: { items: StatItem[] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-xs leading-5 sm:grid-cols-[auto_1fr_auto_1fr]">
      {items.map((it) => (
        <div key={it.label} className="contents">
          <dt className="text-slate-400">{it.label}:</dt>
          <dd className={`font-medium tabular-nums ${it.tone ?? 'text-slate-800'}`}>
            {it.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/** 底部快捷键栏的一项。 */
export interface KeyHint {
  /** 按键名，渲染成 <k> 样式 */
  key: string
  /** 这个键干什么 */
  action: string
  /** 没绑实现时置灰，不假装可用 */
  disabled?: boolean
}

/**
 * lazydocker 底部那条：`<key> action` 密排一行。
 * 只列真正绑了实现的键——列出不能用的键比不列更糟。
 */
export function KeyBar({ hints }: { hints: KeyHint[] }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-slate-200 bg-slate-50 px-3 py-1.5 font-mono text-xs">
      {hints.map((h) => (
        <span
          key={h.key}
          className={`flex items-center gap-1 ${h.disabled ? 'opacity-40' : ''}`}
        >
          <kbd className="rounded border border-slate-300 bg-white px-1 text-sky-700">
            {h.key}
          </kbd>
          <span className="text-slate-500">{h.action}</span>
        </span>
      ))}
    </div>
  )
}

/**
 * 一个带标题条的面板。标题条用灰底 + 全大写等宽，
 * 右侧可以挂一段补充说明（k9s 表头右上角放计数那种）。
 */
export function Panel({
  title,
  meta,
  children,
  footer,
}: {
  title: string
  meta?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <section
      aria-label={title}
      className="overflow-hidden rounded border border-slate-300 bg-white"
    >
      <header className="flex items-baseline justify-between gap-3 border-b border-slate-300 bg-slate-100 px-3 py-1.5">
        <h2 className="font-mono text-xs font-semibold uppercase tracking-wider text-slate-700">
          {title}
        </h2>
        {meta ? <div className="font-mono text-xs text-slate-500">{meta}</div> : null}
      </header>
      {children}
      {footer}
    </section>
  )
}
