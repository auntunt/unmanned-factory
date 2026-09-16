type SkillRef = { id: string; version: number }
export type Skill = SkillRef & { name: string; source?: {type?: string; agent_name?: string; agent_id?: string}; actor?: string; instructions?: string; description?: string }
export function skillLabel(skill: Skill) {
  if (!skill.id.startsWith('legacy-')) return skill.name
  const summary = (skill.instructions || skill.description || '').replace(/^---[\s\S]*?---\s*/, '').replace(/^#+\s*/gm, '').trim().split(/[。！？\n]/)[0].slice(0, 60)
  const detail = skill.source?.agent_name || summary || skill.source?.agent_id || skill.id
  return `${skill.name} · ${detail}（${skill.id.startsWith('legacy-') && skill.id.length > 16 ? skill.id.slice(-8) : skill.id}）`
}
