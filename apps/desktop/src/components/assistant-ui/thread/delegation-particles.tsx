import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

import { createRendererLoopPauseController } from '@/lib/renderer-loop-pause'
import { useSessionSlice } from '@/lib/use-session-slice'
import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'

import { CloverParticles } from './clover-particles'
import { $delegationAnchors } from './delegation-anchor'

// Weakly keyed by the authoritative array's worker objects would replay on every
// progress update. Session + worker id survives remounts; bounded window memory.
const presented = new Map<string, Set<string>>()
const MAX_BERTHS = 4
// Only travel needs display-rate updates. Keep the settled lane's geometry checks cheap.
const IDLE_FRAME_MS = 1000 / 24
const SPLIT_MS = 650
const DOCK_MS = 850
const smoothTravel = (progress: number) => progress ** 3 * (progress * (progress * 6 - 15) + 10)

function claim(sessionId: string, agent: SubagentProgress, turn?: number) {
  let seen = presented.get(sessionId)

  if (!seen) {
    seen = new Set()
    presented.set(sessionId, seen)

    if (presented.size > 256) {
      presented.delete(presented.keys().next().value!)
    }
  }

  if (seen.has(agent.id)) {
    return false
  }

  seen.add(agent.id)

  return (agent.status === 'running' || agent.status === 'queued') && turn !== undefined && agent.startedAt >= turn
}

/** A measured visual continuation. Docking does not write to the worker store. */
function ParticleJourney({
  agent,
  index,
  sessionId,
  berth
}: {
  agent: SubagentProgress
  index: number
  sessionId: string
  berth: React.RefObject<HTMLSpanElement | null>
}) {
  const moving = useRef<HTMLSpanElement>(null)
  const anchors = useStore($delegationAnchors)
  const anchor = anchors[sessionId]

  const journey = useRef({
    eligible: false,
    initialized: false,
    docked: false,
    elapsed: 0,
    docking: -1,
    x: 0,
    y: 0,
    fromX: 0,
    fromY: 0,
    sourcePose: null as null | { left: number; top: number; bottom: number; scrollX: number; scrollY: number }
  })

  const wakeRef = useRef<() => void>(() => {})

  // eslint-disable-next-line no-restricted-syntax -- installs an imperative wake callback; atom values are read with get()
  useEffect(() => {
    const node = moving.current
    const target = berth.current

    if (!node || !target) {
      return
    }

    const state = journey.current

    let timer: number | undefined
    let animationFrame: number | undefined
    let visible = true
    let last = performance.now()
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)')

    const stop = () => {
      window.clearTimeout(timer)

      if (animationFrame !== undefined) {
        window.cancelAnimationFrame(animationFrame)
      }

      timer = undefined
      animationFrame = undefined
    }

    const settle = () => {
      state.docked = true
      node.style.visibility = 'hidden'
      target.style.visibility = 'visible'
      stop()
    }

    const tick = () => {
      stop()

      if (!state.initialized) {
        const turn = $delegationAnchors.get()[sessionId]?.turnStartedAt

        if (turn === undefined) {
          node.style.visibility = 'hidden'
          target.style.visibility = 'visible'

          return
        }

        const current = $subagentsBySession.get()[sessionId]?.find(item => item.id === agent.id) ?? agent
        state.initialized = true
        state.eligible = claim(sessionId, current, turn)
      }

      if (pause.isPaused() || !visible) {
        node.style.visibility = 'hidden'
        target.style.visibility = 'visible'

        return
      }

      if (!state.eligible || state.docked || motion.matches) {
        settle()

        return
      }

      const now = performance.now()
      state.elapsed += Math.min(70, now - last)
      last = now
      const source = $delegationAnchors.get()[sessionId]?.element
      const dest = target.getBoundingClientRect()

      if (source?.isConnected && state.docking < 0) {
        const rect = source.getBoundingClientRect()
        state.sourcePose = { left: rect.left, top: rect.top, bottom: rect.bottom, scrollX: window.scrollX, scrollY: window.scrollY }
      }

      const pose = state.sourcePose

      if (!pose && state.docking < 0) {
        // No measured origin: never invent a flight from (0, 0).
        settle()

        return
      }

      if (pose && state.docking < 0 && (source?.isConnected || state.elapsed < SPLIT_MS)) {
        const rect = {
          left: pose.left + pose.scrollX - window.scrollX,
          top: pose.top + pose.scrollY - window.scrollY,
          bottom: pose.bottom + pose.scrollY - window.scrollY
        }

        // A reserved 64px-high lane, to the right of the stable main formation.
        const progress = Math.min(1, state.elapsed / SPLIT_MS)
        const ease = smoothTravel(progress)
        state.x = rect.left + 32 + (48 + index * 30) * ease
        state.y = rect.top + 32 - Math.sin(ease * Math.PI) * 7

        if (rect.bottom < 0 || rect.top > window.innerHeight) {
          node.style.visibility = 'hidden'
          target.style.visibility = 'visible'

          return
        }
      } else {
        if (state.docking < 0) {
          state.docking = state.elapsed
          state.fromX = state.x
          state.fromY = state.y
        }

        const progress = Math.min(1, (state.elapsed - state.docking) / DOCK_MS)
        const ease = smoothTravel(progress)
        state.x = state.fromX + (dest.left + dest.width / 2 - state.fromX) * ease - Math.sin(ease * Math.PI) * 18
        state.y = state.fromY + (dest.top + dest.height / 2 - state.fromY) * ease

        if (progress >= 1) {
          settle()

          return
        }
      }

      // Text always wins over decoration, including during the glide. The lane
      // itself has no text. Read all geometry before writing transforms.
      const textNodes = document.querySelectorAll(
        '[data-slot="aui_assistant-message-content"] p, [data-slot="aui_assistant-message-content"] pre, [data-slot="aui_user-message-root"], [data-conversation-scaffold], summary'
      )

      const occluded = Array.from(textNodes).some(el => {
        if (source?.contains(el) || el.contains(target)) {
          return false
        }

        const r = el.getBoundingClientRect()

        return (
          r.width > 1 &&
          r.height > 1 &&
          state.x + 14 > r.left &&
          state.x - 14 < r.right &&
          state.y + 14 > r.top &&
          state.y - 14 < r.bottom
        )
      })

      node.style.transform = `translate3d(${state.x - 13}px,${state.y - 13}px,0)`
      node.style.visibility = occluded ? 'hidden' : 'visible'
      target.style.visibility = 'hidden'

      // Travel follows the display clock, not a 24fps timer. Do not run a
      // permanent display-rate loop once the mini is hovering beside its parent.
      if (state.elapsed < SPLIT_MS || state.docking >= 0) {
        animationFrame = window.requestAnimationFrame(tick)
      } else {
        timer = window.setTimeout(tick, IDLE_FRAME_MS)
      }
    }

    const wake = () => {
      last = performance.now()
      tick()
    }

    const pause = createRendererLoopPauseController(wake, { pauseWhenUnfocused: false })

    const observer =
      typeof IntersectionObserver === 'undefined'
        ? null
        : new IntersectionObserver(entries => {
            visible = entries.some(entry => entry.isIntersecting)
            wake()
          })

    observer?.observe(target)
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(wake)
    resize?.observe(target)
    window.addEventListener('resize', wake)
    window.addEventListener('scroll', wake, true)
    motion.addEventListener('change', wake)
    wakeRef.current = wake
    wake()

    return () => {
      stop()
      pause.dispose()
      observer?.disconnect()
      resize?.disconnect()
      window.removeEventListener('resize', wake)
      window.removeEventListener('scroll', wake, true)
      motion.removeEventListener('change', wake)
      target.style.visibility = ''

      wakeRef.current = () => {}
    }
    // The event object's updates must not restart the animation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, agent.id, index, berth])

  useEffect(() => {
    wakeRef.current()
  }, [anchor])

  return createPortal(
    <span aria-hidden="true" className="delegation-flight delegation-particle" ref={moving}>
      <CloverParticles phase="waiting" static={agent.status !== 'running' && agent.status !== 'queued'} />
    </span>,
    document.body
  )
}

function Berth({ agent, index, sessionId }: { agent: SubagentProgress; index: number; sessionId: string }) {
  const ref = useRef<HTMLSpanElement>(null)

  return (
    <>
      <span
        aria-hidden="true"
        className="delegation-particle"
        data-helper-berth={agent.id}
        data-status={agent.status}
        ref={ref}
      >
        <CloverParticles phase="waiting" static={agent.status !== 'running' && agent.status !== 'queued'} />
      </span>
      <ParticleJourney agent={agent} berth={ref} index={index} sessionId={sessionId} />
    </>
  )
}

export function DelegationBerths({ sessionId }: { sessionId: string }) {
  const agents = useSessionSlice($subagentsBySession, sessionId)

  return (
    <span aria-hidden="true" className="delegation-berths">
      {agents.slice(0, MAX_BERTHS).map((agent, index) => (
        <Berth agent={agent} index={index} key={`${sessionId}:${agent.id}`} sessionId={sessionId} />
      ))}
      {agents.length > MAX_BERTHS && <span className="text-xs">+{agents.length - MAX_BERTHS}</span>}
    </span>
  )
}
