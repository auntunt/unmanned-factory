import { useState } from 'react'

export type ChecksMap = Record<string, string[]>

interface ChecksEditorProps {
  value: ChecksMap
  onChange: (value: ChecksMap) => void
}

function nextName(value: ChecksMap): string {
  let index = Object.keys(value).length + 1
  while (Object.prototype.hasOwnProperty.call(value, `check_${index}`)) index += 1
  return `check_${index}`
}

export function validateChecks(value: ChecksMap): string | null {
  if (Object.keys(value).length > 20) return '最多配置 20 条检查。'
  for (const [name, argv] of Object.entries(value)) {
    if (!/^[A-Za-z0-9_-]{1,60}$/.test(name)) return `检查名称“${name || '（空）'}”只能使用字母、数字、下划线和短横线。`
    if (!Array.isArray(argv) || argv.length === 0 || argv.length > 100 || !argv[0]) return `请填写“${name}”的程序，并确保参数数量不超过 100 个。`
    if (argv.some((part) => typeof part !== 'string' || !part || part.length > 4096 || part.includes('\0'))) return `“${name}”的每个参数不能为空、不能超过 4096 个字符，也不能包含空字符。`
  }
  return null
}

export default function ChecksEditor({ value, onChange }: ChecksEditorProps) {
  const [jsonDraft, setJsonDraft] = useState('')
  const [importError, setImportError] = useState<string | null>(null)
  const [nameError, setNameError] = useState<string | null>(null)
  const checks = Object.entries(value)

  const addCheck = () => onChange({ ...value, [nextName(value)]: [''] })
  const rename = (oldName: string, newName: string) => {
    if (newName !== oldName && Object.prototype.hasOwnProperty.call(value, newName)) { setNameError(`检查名称“${newName}”已经存在。`); return }
    const next: ChecksMap = {}
    for (const [name, argv] of Object.entries(value)) next[name === oldName ? newName : name] = argv
    setNameError(null); onChange(next)
  }
  const updateArg = (name: string, index: number, text: string) => onChange({ ...value, [name]: value[name].map((part, partIndex) => partIndex === index ? text : part) })
  const addArg = (name: string) => onChange({ ...value, [name]: [...value[name], ''] })
  const removeArg = (name: string, index: number) => onChange({ ...value, [name]: value[name].filter((_, partIndex) => partIndex !== index) })
  const removeCheck = (name: string) => { const next = { ...value }; delete next[name]; onChange(next) }

  const importJson = () => {
    try {
      const parsed: unknown = JSON.parse(jsonDraft)
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('需要一个名称到参数数组的对象。')
      const next = parsed as Record<string, unknown>
      const result: ChecksMap = {}
      for (const [name, argv] of Object.entries(next)) {
        if (!Array.isArray(argv) || argv.some((part) => typeof part !== 'string')) throw new Error(`“${name}”必须是文本参数数组。`)
        result[name] = argv
      }
      const error = validateChecks(result)
      if (error) throw new Error(error)
      onChange(result); setImportError(null)
    } catch (error) { setImportError(error instanceof Error ? error.message : 'JSON 导入失败。') }
  }

  return <div className="wb-check-editor">
    <div className="wb-check-editor-head"><div><strong>验收检查</strong><p>每个参数单独填写，不做 shell 解析，空格和引号会按原样传给程序。</p></div><button className="wb-button wb-button-secondary" type="button" onClick={addCheck}>添加检查</button></div>
    {checks.length === 0 && <div className="wb-check-empty"><strong>还没有检查</strong><span>可以先暂存项目；规划和执行前需要至少一项可信检查。</span><button className="wb-button wb-button-secondary" type="button" onClick={addCheck}>添加第一项</button></div>}
    <div className="wb-check-list">{checks.map(([name, argv], checkIndex) => <div className="wb-check-row" key={checkIndex}><div className="wb-check-row-head"><label>检查名称<input value={name} onChange={(event) => rename(name, event.target.value)} aria-label={`${name} 检查名称`} /></label><button className="wb-remove-button" type="button" onClick={() => removeCheck(name)}>删除</button></div><div className="wb-argv-label">程序与参数</div><div className="wb-argv-list">{argv.map((part, index) => <div className="wb-argv-row" key={index}><input value={part} onChange={(event) => updateArg(name, index, event.target.value)} aria-label={`${name} 参数 ${index + 1}`} placeholder={index === 0 ? '程序，例如 pytest' : `参数 ${index}`} />{argv.length > 1 && <button className="wb-remove-button" type="button" onClick={() => removeArg(name, index)} aria-label={`删除 ${name} 参数 ${index + 1}`}>×</button>}</div>)}</div><button className="wb-add-arg" type="button" onClick={() => addArg(name)}>＋ 添加参数</button></div>)}</div>
    <details className="wb-check-import"><summary>高级：导入 JSON 配置</summary><div className="wb-check-import-body"><textarea rows={4} value={jsonDraft} onChange={(event) => { setJsonDraft(event.target.value); setImportError(null) }} placeholder={'{"test":["pytest","-q"]}'} /><div className="wb-check-import-actions"><button className="wb-button wb-button-secondary" type="button" disabled={!jsonDraft.trim()} onClick={importJson}>导入到编辑器</button>{importError && <span className="wb-check-import-error" role="alert">{importError}</span>}</div></div></details>
    {(nameError || validateChecks(value)) && <p className="wb-check-editor-error" role="alert">{nameError || validateChecks(value)}</p>}
  </div>
}
