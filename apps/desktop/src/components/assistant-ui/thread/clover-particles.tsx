import { useEffect, useRef } from 'react'

import { createRendererLoopPauseController } from '@/lib/renderer-loop-pause'

export type CloverPhase = 'working' | 'tool' | 'waiting' | 'compacting'

const SIZE = 224
const FRAME_MS = 1000 / 24

// Four heart-shaped leaves, not a rotating ring. Deterministic samples keep the
// formation stable across renders and avoid random movement suggesting progress.
const leaves = Array.from({ length: 704 }, (_, i) => {
  const leaf = Math.floor(i / 176)
  const angle = i * 2.399963229728653
  const radius = Math.sqrt((((i * 73) % 176) + 0.5) / 176)
  const x = 16 * Math.sin(angle) ** 3 * radius * 1.58
  const y =
    -(13 * Math.cos(angle) - 5 * Math.cos(2 * angle) - 2 * Math.cos(3 * angle) - Math.cos(4 * angle)) * radius * 1.58 -
    27
  const rotation = Math.PI / 4 + (leaf * Math.PI) / 2

  return {
    x: x * Math.cos(rotation) - y * Math.sin(rotation),
    y: x * Math.sin(rotation) + y * Math.cos(rotation),
    z: Math.sin(i * 1.37) * 7,
    phase: angle,
    tone: i % 4
  }
})

const field = Array.from({ length: 72 }, (_, i) => {
  const angle = i * 2.399963229728653
  const radius = 65 + ((i * 17) % 37)

  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, phase: angle }
})

/** Decoration for authoritative session activity. A phase changes the gesture,
 * never the status copy, reasoning, completion estimate or model state.
 * Supports staged formation on enter, continuous breathing settle, and graceful
 * departure on exit without abrupt popping or fake progress indicators. */
export function CloverParticles({
  phase = 'working',
  departing = false,
  static: staticParticle = false
}: {
  phase?: CloverPhase
  departing?: boolean
  static?: boolean
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const phaseRef = useRef(phase)
  phaseRef.current = phase
  const departingRef = useRef(departing)
  departingRef.current = departing
  const staticRef = useRef(staticParticle)
  staticRef.current = staticParticle

  useEffect(() => {
    const canvas = ref.current

    if (!canvas) {
      return
    }
    const context = canvas.getContext('2d')

    if (!context) {
      return
    }
    const scale = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = SIZE * scale
    canvas.height = SIZE * scale
    context.scale(scale, scale)
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)')
    let visible = true
    let timer: number | undefined
    let frame = 0
    let exitFrame = 0

    // Resolve the existing theme; no fixed mint that breaks light/custom skins.
    const ink = getComputedStyle(canvas).color

    const draw = () => {
      frame++
      const state = phaseRef.current
      const isDeparting = departingRef.current

      if (!isDeparting) {
        exitFrame = 0
      }

      // Waiting is a live, calm gesture, not a completed or paused renderer.
      const still = motion.matches || staticRef.current
      const time = still ? 0 : frame / 24

      // Staged enter formation: cubic ease-out over first 14 frames (~350ms)
      const formProgress = still ? 1 : Math.min(1, frame / 14)
      const formEase = 1 - Math.pow(1 - formProgress, 3)
      const formScale = 0.88 + 0.12 * formEase

      // Departure exit: quadratic fade-out and gentle upward dispersal
      if (isDeparting && !motion.matches) {
        exitFrame++
      }

      const exitProgress = motion.matches ? (isDeparting ? 1 : 0) : Math.min(1, exitFrame / 10)
      const exitEase = Math.pow(exitProgress, 2)
      const exitAlpha = Math.max(0, 1 - exitEase)
      const exitScale = 1 - 0.05 * exitEase
      const exitY = -exitEase * 6

      if (isDeparting && exitProgress >= 1) {
        stop()
        context.clearRect(0, 0, SIZE, SIZE)

        return
      }

      const alphaBase = formEase * (isDeparting ? exitAlpha : 1)

      context.clearRect(0, 0, SIZE, SIZE)

      if (alphaBase <= 0.001) {
        return
      }

      if (typeof context.save === 'function') {
        context.save()
      }
      const totalScale = formScale * (isDeparting ? exitScale : 1)

      if (typeof context.translate === 'function' && (totalScale !== 1 || (isDeparting && exitY !== 0))) {
        context.translate(112, 112 + (isDeparting ? exitY : 0))
        context.scale(totalScale, totalScale)
        context.translate(-112, -112)
      }

      const breath = Math.sin(time * 1.6) * (state === 'compacting' ? 0.07 : 0.025) + 1
      const yaw = Math.sin(time * 0.65) * 0.22
      const tilt = -0.12 + Math.sin(time * 0.5) * 0.035

      context.fillStyle = ink

      // One path per tone, rather than one fill/style allocation per particle.
      for (let tone = 0; tone < 4; tone++) {
        context.globalAlpha = (0.35 + tone * 0.19) * alphaBase
        context.beginPath()

        for (const p of leaves) {
          if (p.tone !== tone) {
            continue
          }
          const ripple = still
            ? 0
            : Math.sin(time * (state === 'tool' ? 3.2 : 1.8) + p.phase) * (state === 'tool' ? 2 : 0.8)
          const x = (p.x * Math.cos(yaw) + p.z * Math.sin(yaw)) * breath
          const y = p.y * breath * 0.9 + p.z * 0.55
          const px = 112 + (x * Math.cos(tilt) - y * Math.sin(tilt) + ripple) * 1.65
          const py = 106 + (x * Math.sin(tilt) + y * Math.cos(tilt) + ripple * 0.4) * 1.65
          const radius = tone === 3 ? 1.15 : 0.85
          context.moveTo(px + radius, py)
          context.arc(px, py, radius, 0, Math.PI * 2)
        }

        context.fill()
      }

      context.globalAlpha = 0.28 * alphaBase
      context.beginPath()

      for (const p of field) {
        const drift = still ? 0 : Math.sin(time * 0.6 + p.phase) * 4
        const x = 112 + p.x + drift
        const y = 108 + p.y * 0.85 + drift
        context.moveTo(x + 0.65, y)
        context.arc(x, y, 0.65, 0, Math.PI * 2)
      }

      context.fill()

      // A stippled stem grounds the object; no shadow/rim/filter animation.
      context.globalAlpha = 0.55 * alphaBase
      context.beginPath()

      for (let i = 0; i < 24; i++) {
        const x = 114 + i * 0.62 + Math.sin(i * 2.4) * 1.3
        const y = 137 + i * 1.35
        context.moveTo(x + 0.8, y)
        context.arc(x, y, 0.8, 0, Math.PI * 2)
      }

      context.fill()

      if (typeof context.restore === 'function') {
        context.restore()
      }
    }

    const stop = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer)
      }
      timer = undefined
    }

    const tick = () => {
      draw()
      timer = window.setTimeout(tick, FRAME_MS)
    }

    const update = () => {
      stop()

      if (!visible || document.hidden || pause.isPaused()) {
        return
      }

      if (motion.matches || staticRef.current) {
        draw()
      } else {
        tick()
      }
    }

    const pause = createRendererLoopPauseController(update, { pauseWhenUnfocused: false })

    const observer =
      typeof IntersectionObserver === 'undefined'
        ? null
        : new IntersectionObserver(entries => {
            visible = entries[0]?.isIntersecting ?? false
            update()
          })

    observer?.observe(canvas)
    document.addEventListener('visibilitychange', update)
    motion.addEventListener('change', update)
    canvas.addEventListener('clover-phase-change', update)

    return () => {
      stop()
      pause.dispose()
      observer?.disconnect()
      document.removeEventListener('visibilitychange', update)
      motion.removeEventListener('change', update)
      canvas.removeEventListener('clover-phase-change', update)
    }
  }, [])

  // eslint-disable-next-line no-restricted-syntax -- props select the gesture (including the tool ancestor); no atom state is mirrored
  useEffect(() => {
    const canvas = ref.current

    if (!canvas) {
      return
    }
    const effectivePhase = canvas.closest('[data-particle-tool-activity]') ? 'tool' : phase
    phaseRef.current = effectivePhase
    canvas.dataset.cloverPhase = effectivePhase
    canvas.dispatchEvent(new Event('clover-phase-change'))
  }, [phase, departing, staticParticle])

  return (
    <canvas
      aria-hidden="true"
      className="clover-formation pointer-events-none shrink-0 text-(--theme-primary)"
      data-clover-phase={phase}
      ref={ref}
      style={{ width: SIZE, height: SIZE }}
    />
  )
}
