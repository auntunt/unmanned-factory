import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

export default function Submit() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [taskId, setTaskId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [checkCommand, setCheckCommand] = useState('uv run pytest tests/ -q')
  const [checkTimeout, setCheckTimeout] = useState(300)
  const [maxRounds, setMaxRounds] = useState(3)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setLoading(true)

    try {
      // 基本验证
      if (!taskId.match(/^T-[a-z0-9-]+$/)) {
        throw new Error('task_id 必须是 T- 开头的 kebab-case，如 T-fix-bug-123')
      }
      if (!prompt.trim()) {
        throw new Error('任务描述不能为空')
      }

      // 构造 YAML
      const yaml = `task_id: ${taskId}
prompt: |
${prompt.split('\n').map(line => '  ' + line).join('\n')}

declared_ops:
  - edit_code
  - run_tests

max_rounds: ${maxRounds}

checks:
  - name: main_check
    command: ${checkCommand}
    expect: exit_zero
    timeout_s: ${checkTimeout}

  - name: no_regression
    command: |
      uv run pytest tests/ -q \\
        --deselect tests/test_e2e_smoke.py \\
        --deselect tests/test_parallel.py::test_three_tasks_run_in_parallel_and_all_merge
    expect: exit_zero
    timeout_s: 900
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
      <h1 className="text-2xl font-semibold">投递新任务</h1>
      <p className="mt-2 text-sm text-slate-600">
        填写任务描述和验收判据，提交后 worker 会自动开始处理
      </p>

      {error && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="mt-6 space-y-6">
        {/* Task ID */}
        <div>
          <label className="block text-sm font-medium text-slate-700">
            Task ID <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            placeholder="T-fix-test-bar-failure"
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
            required
          />
          <p className="mt-1 text-xs text-slate-500">
            格式：T- 开头，小写字母、数字、连字符，如 T-fix-login-crash
          </p>
        </div>

        {/* Prompt */}
        <div>
          <label className="block text-sm font-medium text-slate-700">
            任务描述 <span className="text-red-500">*</span>
          </label>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={10}
            placeholder={`修复 tests/test_foo.py::test_bar 失败

失败信息：assert result == 42, got 41

检查 src/calculator.py 的逻辑，可能是 off-by-one。

约束：不要改测试，只改实现。`}
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
            required
          />
          <p className="mt-1 text-xs text-slate-500">
            包含：目标、背景、约束、已知信息。越具体越好。
          </p>
        </div>

        {/* Check Command */}
        <div>
          <label className="block text-sm font-medium text-slate-700">
            验收命令 <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={checkCommand}
            onChange={(e) => setCheckCommand(e.target.value)}
            placeholder="uv run pytest tests/test_target.py -v"
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
            required
          />
          <p className="mt-1 text-xs text-slate-500">
            验证修复目标的命令，如 pytest 测试、编译命令等
          </p>
        </div>

        {/* Check Timeout */}
        <div>
          <label className="block text-sm font-medium text-slate-700">验收超时（秒）</label>
          <input
            type="number"
            value={checkTimeout}
            onChange={(e) => setCheckTimeout(parseInt(e.target.value))}
            min={30}
            max={3600}
            className="mt-1 block w-32 rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
          />
        </div>

        {/* Max Rounds */}
        <div>
          <label className="block text-sm font-medium text-slate-700">最大尝试轮数</label>
          <input
            type="number"
            value={maxRounds}
            onChange={(e) => setMaxRounds(parseInt(e.target.value))}
            min={1}
            max={5}
            className="mt-1 block w-32 rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
          />
          <p className="mt-1 text-xs text-slate-500">
            3 轮：haiku → sonnet → opus；失败后升级给人工
          </p>
        </div>

        {/* Submit */}
        <div className="flex gap-3 border-t border-slate-200 pt-6">
          <button
            type="submit"
            disabled={loading}
            className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700 disabled:bg-slate-300"
          >
            {loading ? '投递中...' : '投递任务'}
          </button>
          <button
            type="button"
            onClick={() => navigate('/')}
            className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            取消
          </button>
        </div>
      </form>
    </div>
  )
}
