import { useCallback, useEffect, useRef, useState } from 'react'

/** usePolling 的返回形状。T03 / T05 也依赖这个签名，改动前先同步。 */
export interface PollingResult<T> {
  /** 最近一次成功拉到的数据。轮询失败不会把它清空。 */
  data: T | null
  /** 最近一次失败的原因（成功后清空）。字符串，方便直接渲染。 */
  error: string | null
  /** 最近一次成功刷新的时刻，未成功过时为 null。 */
  lastUpdated: Date | null
  /** 仅首次加载（还没有任何数据）时为 true，后续轮询不闪 loading。 */
  loading: boolean
  /** 手动触发一次刷新，不影响定时器节奏。 */
  refresh: () => void
}

/**
 * 定时轮询一个异步 fetcher。
 *
 * StrictMode 安全：fetcher 存在 ref 里，effect 只依赖 intervalMs，
 * 所以调用方传内联箭头函数也不会每次 render 重建定时器；
 * cleanup 里 clearInterval + cancelled 标记，双挂载不会留下两个定时器。
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
): PollingResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [loading, setLoading] = useState(true)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  // 每次 effect 运行有自己的 generation；旧 generation 的响应一律丢弃。
  const genRef = useRef(0)
  const [manualTick, setManualTick] = useState(0)
  const gateRef = useRef(createPollingGate(() => fetcherRef.current()))

  const refresh = useCallback(() => {
    // A successful login can explicitly resume polling after a 401. Normal
    // interval ticks stay stopped until then, avoiding an auth failure storm.
    gateRef.current.resume()
    setManualTick((n) => n + 1)
  }, [])

  useEffect(() => {
    genRef.current += 1
    const gen = genRef.current
    let timer: ReturnType<typeof setInterval> | undefined

    const run = async () => {
      try {
        const result = await gateRef.current.run()
        if (result.stopped) return
        const next = result.value
        if (genRef.current !== gen) return
        setData(next)
        setError(null)
        setLastUpdated(new Date())
      } catch (err) {
        if (genRef.current !== gen) return
        setError(toMessage(err))
      } finally {
        if (genRef.current === gen) setLoading(false)
      }
    }

    void run()
    if (intervalMs > 0) {
      timer = setInterval(() => {
        void run()
      }, intervalMs)
    }

    return () => {
      genRef.current += 1
      if (timer !== undefined) clearInterval(timer)
    }
  }, [intervalMs, manualTick])

  return { data, error, lastUpdated, loading, refresh }
}

function toMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  return '未知错误'
}

export function isUnauthorized(err: unknown): boolean {
  if (typeof err !== 'object' || err === null) return false
  const value = err as { status?: unknown; response?: { status?: unknown } }
  return value.status === 401 || value.response?.status === 401
}

export function createPollingGate<T>(fetcher: () => Promise<T>) {
  let stopped = false
  let generation = 0
  return {
    async run(): Promise<{ stopped: true } | { stopped: false; value: T }> {
      if (stopped) return { stopped: true }
      const current = generation
      try {
        return { stopped: false, value: await fetcher() }
      } catch (error) {
        if (current === generation && isUnauthorized(error)) stopped = true
        throw error
      }
    },
    resume() { generation += 1; stopped = false },
  }
}

export default usePolling
