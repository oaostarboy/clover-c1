import { useEffect, useRef } from 'react'
import { createRendererLoopPauseController } from '../../lib/renderer-loop-pause'
import { cn } from '@/lib/utils'

import './landing.css'
import { drawBotanical } from './botanical-surface'
export { drawBotanical } from './botanical-surface'

/** Decorative canvas, rendered off the UI thread and paused when not visible.
 * When frosted during chat, softly dimmed and layered behind a frosted veil. */
export function LandingBloom({
  frosted = false,
  className
}: {
  frosted?: boolean
  className?: string
} = {}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const host = ref.current
    if (!host) return
    const media = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    let worker: Worker | undefined
    let canvas: HTMLCanvasElement
    let visible = true, disposed = false
    const makeCanvas = () => {
      const element = document.createElement('canvas')
      element.setAttribute('aria-hidden', 'true')
      element.setAttribute('data-landing-botanical', '')
      Object.assign(element.style, { position: 'absolute', inset: '0', width: '100%', height: '100%', pointerEvents: 'none' })
      host.replaceChildren(element)
      return element
    }
    canvas = makeCanvas()
    const resize = () => {
      const rect = host.getBoundingClientRect()
      const width = Math.max(1, Math.round(rect.width)), height = Math.max(1, Math.round(rect.height))
      const dpr = Math.min(window.devicePixelRatio || 1, 2, 2400 / width, 1600 / height)
      if (worker) worker.postMessage({ type: 'resize', width, height, dpr })
      else {
        canvas.width = Math.max(1, Math.round(width * dpr)); canvas.height = Math.max(1, Math.round(height * dpr))
        const ctx = canvas.getContext('2d')
        if (ctx) { ctx.setTransform(dpr, 0, 0, dpr, 0, 0); drawBotanical(ctx, width, height, 0) }
      }
    }
    const fallback = () => {
      if (disposed) return
      worker?.terminate(); worker = undefined
      canvas = makeCanvas()
      resize()
    }
    try {
      if (typeof Worker !== 'undefined' && 'transferControlToOffscreen' in canvas) {
        worker = new Worker(new URL('./landing-bloom.worker.ts', import.meta.url), { type: 'module' })
        worker.onerror = fallback
        worker.onmessageerror = fallback
        const offscreen = canvas.transferControlToOffscreen()
        worker.postMessage({ type: 'init', canvas: offscreen }, [offscreen])
      }
    } catch { fallback() }
    let pause: ReturnType<typeof createRendererLoopPauseController>
    const update = () => worker?.postMessage({ type: 'active', active: visible && !document.hidden && !pause?.isPaused() && !media?.matches, reduced: !!media?.matches })
    pause = createRendererLoopPauseController(update, { pauseWhenUnfocused: false })
    const sizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize)
    const visibilityObserver = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => {
      visible = entries[0]?.isIntersecting ?? true
      update()
    })
    sizeObserver?.observe(host); visibilityObserver?.observe(host)
    window.addEventListener('resize', resize)
    document.addEventListener('visibilitychange', update)
    media?.addEventListener('change', update)
    resize(); update()
    return () => {
      disposed = true
      worker?.terminate()
      pause.dispose()
      sizeObserver?.disconnect(); visibilityObserver?.disconnect()
      window.removeEventListener('resize', resize)
      document.removeEventListener('visibilitychange', update)
      media?.removeEventListener('change', update)
      host.replaceChildren()
    }
  }, [])
  return (
    <div
      aria-hidden="true"
      data-landing-bloom=""
      data-frosted={frosted ? 'true' : undefined}
      className={cn('pointer-events-none absolute inset-0 z-0 overflow-hidden', className)}
    >
      <div ref={ref} aria-hidden="true" className="absolute inset-0 pointer-events-none" />
      <div
        aria-hidden="true"
        data-slot="landing-bloom-veil"
        className="pointer-events-none absolute inset-0"
      />
    </div>
  )
}
