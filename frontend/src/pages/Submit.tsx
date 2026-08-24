import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

// 输入框：方角、等宽、聚焦时描边而不是加光晕。三处共用。
const FIELD =
  'mt-1 block w-full border border-slate-300 bg-white px-2 py-1.5 font-mono ' +
  'text-xs text-slate-800 focus:border-cyan-600 focus:outline-none ' +
  'focus:ring-1 focus:ring-cyan-600'

export default function Submit() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [taskId, setTaskId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [maxRounds, setMaxRounds] = useState(3)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setLoading(true)

    try {
      // 基本验证。
      // 报错要指名道姓：原来只回一句「必须是 kebab-case」，用户盯着
      // T-FED-updata-001 看不出问题在三个大写字母上 —— 格式描述里
      // 「小写字母」四个字混在一串条件中间，很容易滑过去。
      if (!taskId.startsWith('T-')) {
        throw new Error(`task_id 必须以 T- 开头，当前是「${taskId}」`)
      }
      // 只检查 T- 之后的部分：前缀那个 T 本来就该大写，
      // 连它一起判会把 T-fed_update 误报成「含大写字母 T」，
      // 还会建议改成 t-fed_update —— 把合法前缀也毁掉。
      const rest = taskId.slice(2)
      if (/[A-Z]/.test(rest)) {
        const upper = [...new Set(rest.match(/[A-Z]/g) ?? [])].join('')
        throw new Error(
          `task_id 不能含大写字母（${upper}）。改成 T-${rest.toLowerCase()} 即可`,
        )
      }
      if (!taskId.match(/^T-[a-z0-9-]+$/)) {
        const bad = [...new Set(taskId.slice(2).match(/[^a-z0-9-]/g) ?? [])].join('')
        throw new Error(
          bad
            ? `task_id 含不允许的字符（${bad}），只能用小写字母、数字、连字符`
            : 'task_id 的 T- 后面不能为空，如 T-fix-bug-123',
        )
      }
      if (!prompt.trim()) {
        throw new Error('任务描述不能为空')
      }

      // 构造最小 YAML：只有目标，AI 自己决定怎么验证
      const yaml = `task_id: ${taskId}
prompt: |
${prompt.split('\n').map(line => '  ' + line).join('\n')}

max_rounds: ${maxRounds}
`

      // 调用后端 API 投递
      const res = await fetch('/api/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, yaml }),
      })

      if (!res.ok) {
        const errData = await res.json().catch(() => ({ error: res.statusText }))
        throw new Error(errData.error || `HTTP ${res.status}`)
      }

      // 成功，跳转到任务列表
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl">
      {/* 终端标题栏，与看板同一套外观 */}
      <header className="border border-slate-800 bg-slate-800 px-2 py-1">
        <h1 className="font-mono text-xs font-bold uppercase tracking-widest text-white">
          factory/submit
        </h1>
      </header>
      <p className="border-x border-b border-slate-300 bg-slate-50 px-2 py-1 font-mono text-xs text-slate-500">
        只需描述目标，AI 会自己决定怎么验证、怎么改代码
      </p>

      {error && (
        <div
          role="alert"
          className="border-x border-b border-l-2 border-slate-300 border-l-red-600 bg-red-50 px-2 py-1 font-mono text-xs"
        >
          <span className="font-bold text-red-700">ERR</span>{' '}
          <span className="text-slate-700">{error}</span>
        </div>
      )}

      <form
        onSubmit={handleSubmit}
        className="space-y-4 border-x border-b border-slate-300 p-3"
      >
        {/* Task ID */}
        <div>
          <label className="block font-mono text-xs font-semibold uppercase tracking-wider text-slate-600">
            Task ID <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            placeholder="T-fix-login-crash"
            className={FIELD}
            required
          />
          <p className="mt-1 font-mono text-xs text-slate-400">
            格式：T- 开头，小写字母、数字、连字符，如 T-fix-login-crash
          </p>
        </div>

        {/* Prompt */}
        <div>
          <label className="block font-mono text-xs font-semibold uppercase tracking-wider text-slate-600">
            任务描述 <span className="text-red-500">*</span>
          </label>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={12}
            placeholder={`修复 tests/test_foo.py::test_bar 失败

失败信息：assert result == 42, got 41

检查 src/calculator.py 的逻辑，可能是 off-by-one。

约束：不要改测试，只改实现。`}
            className={FIELD}
            required
          />
          <p className="mt-1 font-mono text-xs text-slate-400">
            包含：目标、背景、约束、已知信息。AI 会自己决定怎么验证。
          </p>
        </div>

        {/* Max Rounds */}
        <div>
          <label className="block font-mono text-xs font-semibold uppercase tracking-wider text-slate-600">最大尝试轮数</label>
          <input
            type="number"
            value={maxRounds}
            onChange={(e) => setMaxRounds(parseInt(e.target.value))}
            min={1}
            max={5}
            className={`${FIELD} w-24`}
          />
          <p className="mt-1 font-mono text-xs text-slate-400">
            默认 3 轮：sonnet → opus → opus；失败后升级给人工
          </p>
        </div>

        {/* Submit */}
        <div className="flex gap-2 border-t border-slate-300 pt-3">
          <button
            type="submit"
            disabled={loading}
            className="border border-slate-800 bg-slate-800 px-3 py-1.5 font-mono text-xs font-semibold text-white transition hover:bg-slate-700 focus:outline-none focus-visible:ring-1 focus-visible:ring-cyan-500 disabled:border-slate-300 disabled:bg-slate-300"
          >
            {loading ? '[投递中…]' : '[enter] 投递任务'}
          </button>
          <button
            type="button"
            onClick={() => navigate('/')}
            className="border border-slate-300 bg-white px-3 py-1.5 font-mono text-xs text-slate-600 transition hover:bg-slate-50 focus:outline-none focus-visible:ring-1 focus-visible:ring-slate-400"
          >
            [esc] 取消
          </button>
        </div>
      </form>
    </div>
  )
}
