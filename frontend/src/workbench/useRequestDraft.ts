import { useEffect, useState } from 'react'
export interface RequestDraft { value: string; operation: string; fields: Record<string,string> }
const empty: RequestDraft = { value: '', operation: 'general', fields: {} }
export const draftKey = (projectId: string | number) => `webuddy:request:${projectId}`
function read(projectId: string | number): RequestDraft {
  try {
    const value = JSON.parse(localStorage.getItem(draftKey(projectId)) || 'null')
    if (!value || typeof value.value !== 'string') return empty
    const fields = Object.fromEntries(Object.entries(value.fields || {}).filter((entry): entry is [string,string] => typeof entry[1] === 'string').map(([key, text]) => [key,text.slice(0,8000)]))
    return { value: value.value.slice(0,50000), operation: typeof value.operation === 'string' ? value.operation : 'general', fields }
  } catch { return empty }
}
export default function useRequestDraft(projectId: string | number) {
  const [draft, setDraft] = useState<RequestDraft>(() => read(projectId))
  useEffect(() => { try { if (draft.value || Object.values(draft.fields).some(Boolean)) localStorage.setItem(draftKey(projectId), JSON.stringify(draft)); else localStorage.removeItem(draftKey(projectId)) } catch { /* Editing remains available without storage. */ } }, [draft, projectId])
  const clear = () => { try { localStorage.removeItem(draftKey(projectId)) } catch { /* optional storage */ } setDraft(empty) }
  return { draft, setDraft, clear }
}
