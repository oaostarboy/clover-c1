import { useLayoutEffect, useRef } from 'react'

/** Keep the same editor node and focus. Only its visual position travels;
 * layout and scroll clearance adopt the destination immediately. */
export function useConversationContinuation(landing: boolean) {
  const surface = useRef<HTMLDivElement>(null)
  const previous = useRef<DOMRect | null>(null)
  const wasLanding = useRef(landing)
  useLayoutEffect(() => {
    const root = surface.current
    const dock = root?.querySelector<HTMLElement>('[data-slot="composer-dock"]')
    if (!root || !dock) {return}
    let animation: Animation | undefined
    if (wasLanding.current && !landing && previous.current && !dock.hasAttribute('data-popped-out') &&
      !document.hidden && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      const to = dock.getBoundingClientRect()
      const from = previous.current
      // Individual translate composes with the dock's existing centering transform.
      animation = dock.animate([
        { translate: `${from.x + from.width / 2 - to.x - to.width / 2}px ${from.y - to.y}px`, scale: `${from.width / to.width} 1` },
        { translate: '0px 0px', scale: '1 1' }
      ], { duration: 360, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' })
    }
    wasLanding.current = landing
    const measure = () => { if (landing) {previous.current = dock.getBoundingClientRect()} }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(dock)
    const stop = () => animation?.cancel()
    document.addEventListener('visibilitychange', stop)
    return () => { observer.disconnect(); stop(); document.removeEventListener('visibilitychange', stop) }
  }, [landing])
  return surface
}
