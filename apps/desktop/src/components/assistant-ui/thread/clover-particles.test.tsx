import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CloverParticles } from './clover-particles'

describe('CloverParticles', () => {
  let draw: ReturnType<typeof vi.fn>
  let intersection: (entries: { isIntersecting: boolean }[]) => void
  let reduced: boolean
  let mediaChange: () => void

  beforeEach(() => {
    vi.useFakeTimers()
    reduced = false
    draw = vi.fn()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      scale: vi.fn(), clearRect: draw, beginPath: vi.fn(), moveTo: vi.fn(), arc: vi.fn(), fill: vi.fn()
    } as unknown as CanvasRenderingContext2D)
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: typeof intersection) { intersection = callback }
      observe() {}
      disconnect() {}
    })
    vi.stubGlobal('matchMedia', () => ({
      get matches() { return reduced },
      addEventListener: (_: string, callback: () => void) => { mediaChange = callback },
      removeEventListener: vi.fn()
    }))
  })
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

  it('uses a prominent 224px formation rather than a small inline spinner', () => {
    const { container } = render(<CloverParticles />)
    const canvas = container.querySelector('canvas')!
    expect(canvas.width).toBe(224 * Math.min(window.devicePixelRatio || 1, 2))
    expect(canvas.getAttribute('data-clover-phase')).toBe('working')
  })

  it('uses the authoritative active tool marker for its tool gesture', () => {
    const { container } = render(<div data-particle-tool-activity=""><CloverParticles /></div>)
    expect(container.querySelector('canvas')?.getAttribute('data-clover-phase')).toBe('tool')
  })

  it('renders decoratively, animates at a capped rate, and stops on unmount', () => {
    const { container, unmount } = render(<CloverParticles />)
    expect(container.querySelector('canvas')?.getAttribute('aria-hidden')).toBe('true')
    act(() => vi.advanceTimersByTime(1000))
    expect(draw.mock.calls.length).toBeLessThanOrEqual(30)
    unmount()
    const count = draw.mock.calls.length
    act(() => vi.advanceTimersByTime(1000))
    expect(draw).toHaveBeenCalledTimes(count)
  })

  it('pauses on native window hide even when Chromium reports visible', () => {
    let notify: ((state: { isVisible: boolean }) => void) | undefined
    const off = vi.fn()
    vi.stubGlobal('cloverDesktop', undefined)
    Object.defineProperty(window, 'cloverDesktop', { configurable: true, value: {
      onWindowStateChanged: (callback: typeof notify) => { notify = callback; return off }
    } })
    const { unmount } = render(<CloverParticles />)
    act(() => notify?.({ isVisible: false }))
    const count = draw.mock.calls.length
    act(() => vi.advanceTimersByTime(200))
    expect(draw).toHaveBeenCalledTimes(count)
    act(() => notify?.({ isVisible: true }))
    expect(draw.mock.calls.length).toBeGreaterThan(count)
    unmount()
    expect(off).toHaveBeenCalledOnce()
  })

  it('pauses offscreen and resumes when visible', () => {
    render(<CloverParticles />)
    act(() => intersection([{ isIntersecting: false }]))
    const count = draw.mock.calls.length
    act(() => vi.advanceTimersByTime(200))
    expect(draw).toHaveBeenCalledTimes(count)
    act(() => intersection([{ isIntersecting: true }]))
    expect(draw.mock.calls.length).toBeGreaterThan(count)
  })

  it('draws one still frame under reduced motion', () => {
    reduced = true
    render(<CloverParticles />)
    expect(draw).toHaveBeenCalledTimes(1)
    act(() => vi.advanceTimersByTime(1000))
    expect(draw).toHaveBeenCalledTimes(1)
    reduced = false
    act(() => mediaChange())
    act(() => vi.advanceTimersByTime(100))
    expect(draw.mock.calls.length).toBeGreaterThan(1)
  })

  it('pauses while the document is hidden', () => {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    render(<CloverParticles />)
    hidden.mockReturnValue(true)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    const count = draw.mock.calls.length
    act(() => vi.advanceTimersByTime(200))
    expect(draw).toHaveBeenCalledTimes(count)
    hidden.mockReturnValue(false)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(draw.mock.calls.length).toBeGreaterThan(count)
  })
})
