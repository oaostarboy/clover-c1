import { type RefObject, useLayoutEffect } from 'react'

// Clip only transcript ink, never the glass or its ancestors. The outer path
// deliberately extends past the element so shadows and other overflow survive.
export function useGlassOcclusion(
  scrollRef: RefObject<HTMLElement | null>,
  contentRef: RefObject<HTMLElement | null>,
  sessionKey: string | null | undefined,
  empty: boolean
) {
  useLayoutEffect(() => {
    const scroll = scrollRef.current
    const content = contentRef.current
    if (!scroll || !content) {
      return
    }

    let frame = 0
    const previous = new Map<HTMLElement, string>()
    const observed = new Set<HTMLElement>()
    const update = () => {
      frame = 0
      const bubbleElements = [...content.querySelectorAll<HTMLElement>('[data-slot="aui_user-message-root"] .composer-human-message')]
      const bubbles = bubbleElements.map(el => ({ rect: el.getBoundingClientRect(), radius: parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0 }))
      const answers = [...content.querySelectorAll<HTMLElement>('[data-slot="aui_assistant-message-root"]')]
      const writes = answers.map(el => {
        const r = el.getBoundingClientRect()
        const holes = bubbles.filter(({ rect: b }) => b.left < r.right && b.right > r.left && b.top < r.bottom && b.bottom > r.top)
        const path = holes.map(({ rect: b, radius }) => {
          // Backdrop blur samples ink beyond the visible edge of the pill.
          const bleed = 48
          const x = b.left - r.left - bleed, y = b.top - r.top - bleed, w = b.width + bleed * 2, h = b.height + bleed * 2
          const rad = Math.min(radius + bleed, w / 2, h / 2)
          return `M ${x + rad} ${y} H ${x + w - rad} Q ${x + w} ${y} ${x + w} ${y + rad} V ${y + h - rad} Q ${x + w} ${y + h} ${x + w - rad} ${y + h} H ${x + rad} Q ${x} ${y + h} ${x} ${y + h - rad} V ${y + rad} Q ${x} ${y} ${x + rad} ${y} Z`
        }).join(' ')
        return { el, value: path ? `path(evenodd, "M -100000 -100000 H 100000 V 100000 H -100000 Z ${path}")` : '' }
      })
      for (const { el, value } of writes) {
        if (!previous.has(el)) {
          previous.set(el, el.style.clipPath)
        }
        if (el.style.clipPath !== value) {
          el.style.clipPath = value || previous.get(el) || ''
        }
      }
      for (const el of observed) {
        if (!bubbleElements.includes(el)) {
          resize.unobserve(el)
          observed.delete(el)
        }
      }
      for (const el of bubbleElements) {
        if (!observed.has(el)) {
          resize.observe(el)
          observed.add(el)
        }
      }
      for (const el of previous.keys()) {
        if (!el.isConnected) {
          previous.delete(el)
        }
      }
    }
    const schedule = () => {
      if (!frame) {
        frame = requestAnimationFrame(update)
      }
    }
    const resize = new ResizeObserver(schedule)
    resize.observe(scroll)
    resize.observe(content)
    const mutation = new MutationObserver(schedule)
    mutation.observe(content, { childList: true, subtree: true, characterData: true })
    scroll.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    schedule()
    return () => {
      cancelAnimationFrame(frame)
      resize.disconnect()
      mutation.disconnect()
      scroll.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      for (const [el, value] of previous) {
        el.style.clipPath = value
      }
    }
  }, [scrollRef, contentRef, sessionKey, empty])
}
