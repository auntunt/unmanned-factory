import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

// 输入框：方角、等宽、聚焦时描边而不是加光晕。三处共用。
const FIELD =
  'mt-1 block w-full border border-slate-300 bg-white px-2 py-1.5 font-mono ' +
  'text-xs text-slate-800 focus:border-cyan-600 focus:outline-none ' +
  'focus:ring-1 focus:ring-cyan-600'

/** 后端拒投时的响应体（`factory/api.py` 的三段式）。
 *
 * `why` 是逐条理由（闸门可能一次命中多条），`how` 是一句「接下来做什么」。
 * 两个字段都可能缺 —— 网络层错误、反代回的 502 都不长这样，所以渲染前要
 * 兜住 undefined。
 */
type SubmitError = { error: string; why?: string[]; how?: string }

export default function Submit() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  // 后端拒投时回的是三段式 {error, why[], how}。只存 message 会把「为什么」
  // 和「怎么改」丢掉 —— 那两段才是能让人自己修好 YAML 的部分。
  const [error, setError] = useState<SubmitError | null>(null)

  const [taskId, setTaskId] = useState('')
  const [prompt, setPrompt] = useState('')
  // 投递闸门要求「有可核对的验收标准」，所以这一项是必填的。
  // 表单原来不收它，闸门一上线看板投递会 100% 被 400 拦掉。
  const [acceptance, setAcceptance] = useState('')
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
      // 空行和纯空白行不算一条。全是空白时和没填一样。
      const accLines = acceptance
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean)
      if (accLines.length === 0) {
        throw new Error('验收标准不能为空，一行一条（没有它监工无从判断做完没）')
      }

      // 构造 YAML。acceptance 必须在里面：后端闸门和 CLI 入队用同一套规则，
      // 少了它会被判 [no-acceptance] 拒收。
      const yaml = `task_id: ${taskId}
prompt: |
${prompt.split('\n').map(line => '  ' + line).join('\n')}

acceptance:
${accLines.map((l) => `  - ${JSON.stringify(l)}`).join('\n')}

max_rounds: ${maxRounds}
`

      // 调用后端 API 投递
      const res = await fetch('/api/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, yaml }),
      })

      if (!res.ok) {
        const body = await res
          .json()
          .catch(() => ({ error: res.statusText || `HTTP ${res.status}` }))
        // 直接 setError 而不是 throw new Error：Error 只装得下一个字符串，
        // 走一趟 throw 就把 why/how 挤掉了。
        setError({
          error: body.error || `HTTP ${res.status}`,
          why: Array.isArray(body.why) ? body.why : undefined,
          how: typeof body.how === 'string' ? body.how : undefined,
        })
        return
      }

      // 成功，跳转到任务列表
      navigate('/')
    } catch (err) {
      // 走到这里是网络层的问题（连不上、CORS、JSON 炸了），没有三段式可用。
      setError({ error: err instanceof Error ? err.message : String(err) })
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
          <div>
            <span className="font-bold text-red-700">ERR</span>{' '}
            <span className="text-slate-700">{error.error}</span>
          </div>
          {error.why && error.why.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {error.why.map((line, i) => (
                <li key={i} className="text-slate-600">
                  <span className="text-red-600">·</span> {line}
                </li>
              ))}
            </ul>
          )}
          {error.how && (
            <div className="mt-1 text-cyan-800">
              <span className="font-bold">FIX</span> {error.how}
            </div>
          )}
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

        {/* Acceptance —— 必填，因为投递闸门要求它 */}
        <div>
          <label className="block font-mono text-xs font-semibold uppercase tracking-wider text-slate-600">
            验收标准 <span className="text-red-500">*</span>
          </label>
          <textarea
            value={acceptance}
            onChange={(e) => setAcceptance(e.target.value)}
            rows={4}
            placeholder={`一行一条，比如：

foo() 返回 (a, b) 而不是 list
tests/test_foo.py 全绿
不改 tests/ 下的任何文件`}
            className={FIELD}
            required
          />
          <p className="mt-1 font-mono text-xs text-slate-400">
            一行一条，必填。没有可核对的标准，投递闸门会直接拒（后端和 CLI
            同一套规则），而且监工无从判断任务算不算做完。
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
