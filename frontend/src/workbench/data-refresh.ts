const DATA_CHANGED = 'factory:data-changed'
const STORAGE_KEY = 'factory-data-revision'

export function notifyDataChanged() {
  const revision = `${Date.now()}:${Math.random()}`
  try { window.localStorage.setItem(STORAGE_KEY, revision) } catch { /* same-tab event still refreshes */ }
  window.dispatchEvent(new Event(DATA_CHANGED))
}

export function subscribeDataRefresh(refresh: () => void) {
  const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
  const onStorage = (event: StorageEvent) => { if (event.key === STORAGE_KEY) refresh() }
  window.addEventListener('focus', refresh)
  window.addEventListener(DATA_CHANGED, refresh)
  window.addEventListener('storage', onStorage)
  document.addEventListener('visibilitychange', onVisible)
  return () => {
    window.removeEventListener('focus', refresh)
    window.removeEventListener(DATA_CHANGED, refresh)
    window.removeEventListener('storage', onStorage)
    document.removeEventListener('visibilitychange', onVisible)
  }
}
