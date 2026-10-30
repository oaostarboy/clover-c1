import { cn } from '@/lib/utils'

/** Names use the same UI typography as the rest of Clover, not a decorative
 * fitted face. Keep sizing props compatible for existing SDK consumers. */
export function Wordmark({
  className,
  text,
  width = 'calc(100% - 1rem)'
}: {
  className?: string
  /** @deprecated Names no longer scale to fill their container. */
  fitMin?: string
  text: string
  width?: string
}) {
  return (
    <p
      aria-label={text}
      className={cn(
        'wordmark mx-auto max-w-full break-words text-3xl font-medium tracking-tight text-foreground',
        className
      )}
      style={{ width }}
    >
      {text}
    </p>
  )
}
