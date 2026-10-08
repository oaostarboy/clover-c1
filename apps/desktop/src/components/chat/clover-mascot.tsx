import type { CSSProperties } from 'react'

import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

/**
 * The mascot, drawn from `clover-twinkle.webp` (the eight `clover-frames/`
 * stitched into one strip). It twinkles once every few seconds rather than
 * looping constantly: motion that marks "this is Clover", not motion to watch.
 * Reduced motion and a backgrounded window both hold it on the first frame.
 */
export function CloverMascot({
  className,
  size = 96,
  still = false
}: {
  className?: string
  size?: number
  still?: boolean
}) {
  return (
    <span
      aria-hidden="true"
      className={cn('clover-mascot', still && 'clover-mascot--still', className)}
      style={
        {
          '--clover-mascot-size': `${size}px`,
          backgroundImage: `url(${assetPath('clover-twinkle.webp')})`
        } as CSSProperties
      }
    />
  )
}
