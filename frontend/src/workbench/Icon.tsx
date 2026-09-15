import type { SVGProps } from 'react'
import './icons.css'

const paths = {
  "agent": "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM4 21a8 8 0 0 1 16 0M19 5h2M20 4v2",
  "overview": "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  "runs": "M4 4h16v16H4zM9 4v16M15 4v16M4 10h5M9 14h6M15 8h5",
  "project": "M3 7V5h6l2 2h10v13H3z",
  "modules": "m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5",
  "costs": "M4 4v16h17M8 15v-4M13 15V7M18 15v-6",
  "team": "M7 20v-2.5A3.5 3.5 0 0 1 10.5 14h3A3.5 3.5 0 0 1 17 17.5V20M12 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6M18 13a2.5 2.5 0 0 1 2 2.45V18M17 5.5a2.5 2.5 0 0 1 0 4.5",
  "settings": "M4 7h16M4 17h16M8 4v6M16 14v6",
  "external": "M7 17 17 7M7 7h10v10",
  "logout": "M9 4H4v16h5M10 12h11m-4-4 4 4-4 4",
  "menu": "M4 6h16M4 12h16M4 18h16",
  "preview": "M12 3a9 9 0 1 0 9 9M12 7v5l3 2M17 3h4v4",
  "triangle": "m9 5 8 7-8 7Z",
  "back": "m10 5-7 7 7 7M3 12h18",
  "plus": "M12 4v16M4 12h16",
  "arrow": "M3 12h18m-7-7 7 7-7 7",
  "down": "M12 3v18m-7-7 7 7 7-7",
  "download": "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  "style": "M4 4h16v16H4zM4 10h16M10 10v10",
  "knowledge": "M5 3h14v18H5zM8 7h8M8 11h8M8 15h5",
  "workflow": "M3 4h6v6H3zM15 14h6v6h-6zM9 7h9v7",
  "delivery": "M4 5h16v15H4zM8 3v4M16 3v4m-8 7 3 3 5-6"
} as const
export type IconName = keyof typeof paths
export default function Icon({ name, className = '', ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  return <svg {...props} className={`wb-svg-icon ${className}`} viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" data-icon={name}><path d={paths[name]} /></svg>
}

export function CategoryBadge({ category = 'agent', avatar }: { category?: IconName; avatar?: string }) {
  return <span className="wb-category-badge" aria-hidden="true">{avatar?.trim() || <Icon name={category} />}</span>
}
