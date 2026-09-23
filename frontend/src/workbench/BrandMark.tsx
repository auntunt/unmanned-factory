/** One source asset for the app mark and favicon; never substitute a text "w". */
export default function BrandMark({ className = '' }: { className?: string }) {
  return <img className={className} src="/webuddy-mark.svg" width={32} height={32} alt="" aria-hidden="true" draggable={false} />
}
