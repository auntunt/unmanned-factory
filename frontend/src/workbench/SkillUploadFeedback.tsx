import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, type PageProps } from './ui'

export async function uploadSkillAttachment(path: string, file: File, props: PageProps) {
  const body = new FormData()
  body.append('file', file)
  await request(path, { method: 'POST', body, csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized })
}

export default function SkillUploadFeedback({ message }: { message: string }) {
  return <><ErrorNotice message={message} />{message.includes('外部 skill 包') && <Link to="/agents?import=external">改用外部 skill 包·适配与人签</Link>}</>
}
