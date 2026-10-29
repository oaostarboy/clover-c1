import { type ReactNode, useEffect, useState } from 'react'
import { CloverParticles, type CloverPhase } from './clover-particles'

/** A presentation lifetime, not a turn lifetime. No answer or control waits for
 * this timer. The rail is outside text flow and drops accessibility state at once. */
export function ActivityPresence({
  active,
  phase,
  children,
  departDuration = 240
}: {
  active: boolean
  phase: CloverPhase
  children?: ReactNode
  departDuration?: number
}) {
  const [shown, setShown] = useState(active)

  useEffect(() => {
    if (active) {
      setShown(true)
      return
    }
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false
    if (reduced) {
      setShown(false)
      return
    }
    const id = window.setTimeout(() => setShown(false), departDuration)
    return () => window.clearTimeout(id)
  }, [active, departDuration])

  if (!active && !shown) {
    return null
  }

  return (
    <div className="clover-activity-anchor" data-active={active ? '' : undefined}>
      {active && children}
      {shown && (
        <div
          aria-hidden="true"
          className="clover-activity-art"
          data-state={active ? 'present' : 'leaving'}
        >
          <CloverParticles phase={phase} departing={!active} />
        </div>
      )}
    </div>
  )
}
