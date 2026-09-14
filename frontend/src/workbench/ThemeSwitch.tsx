import { useEffect, useState } from 'react'
export type ThemeMode = 'system' | 'light' | 'dark'
export const THEME_KEY = 'webuddy:theme'
export function storedTheme(): ThemeMode {
  try { const value = localStorage.getItem(THEME_KEY); return value === 'light' || value === 'dark' ? value : 'system' } catch { return 'system' }
}
export function applyTheme(mode: ThemeMode) {
  if (mode === 'system') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = mode
}
export default function ThemeSwitch() {
  const [mode, setMode] = useState<ThemeMode>(storedTheme)
  useEffect(() => { applyTheme(mode) }, [mode])
  useEffect(() => { const sync = (e: StorageEvent) => { if (e.key === THEME_KEY || e.key === null) setMode(storedTheme()) }; window.addEventListener('storage', sync); return () => window.removeEventListener('storage', sync) }, [])
  return <label className="wb-theme-switch">外观<select aria-label="外观主题" value={mode} onChange={e => { const value = e.target.value as ThemeMode; setMode(value); applyTheme(value); try { localStorage.setItem(THEME_KEY, value) } catch { /* Theme still works when storage is disabled. */ } }}><option value="system">跟随系统</option><option value="light">亮色</option><option value="dark">暗色</option></select></label>
}
