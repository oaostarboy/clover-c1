import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LandingBloom } from './landing-bloom'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('pauses for offscreen, hidden and reduced motion, then disposes the worker', () => {
  const postMessage = vi.fn(), terminate = vi.fn(), disconnect = vi.fn()
  let visibility!: (entries: { isIntersecting: boolean }[]) => void
  let mediaChange!: () => void
  const media = { matches: false, addEventListener: vi.fn((_name, fn) => { mediaChange = fn }), removeEventListener: vi.fn() }
  vi.stubGlobal('matchMedia', () => media)
  vi.stubGlobal('Worker', class { postMessage = postMessage; terminate = terminate })
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: typeof visibility) { visibility = callback }
    observe() {} disconnect = disconnect
  })
  Object.defineProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen', { configurable: true, value: vi.fn(() => ({})) })
  try {
    const { unmount } = render(<LandingBloom />)
    expect(postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'init' }), expect.any(Array))
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'active', active: true, reduced: false })
    visibility([{ isIntersecting: false }])
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'active', active: false, reduced: false })
    visibility([{ isIntersecting: true }])
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'active', active: true, reduced: false })
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'active', active: false, reduced: false })
    media.matches = true; mediaChange()
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'active', active: false, reduced: true })
    unmount()
    expect(terminate).toHaveBeenCalledOnce()
    expect(disconnect).toHaveBeenCalledOnce()
    expect(media.removeEventListener).toHaveBeenCalledWith('change', mediaChange)
    postMessage.mockClear()
    document.dispatchEvent(new Event('visibilitychange'))
    expect(postMessage).not.toHaveBeenCalled()
  } finally { Reflect.deleteProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen') }
})

it('replaces a failed transferred canvas with a static full-quality fallback', () => {
  let worker!: { onerror?: () => void; terminate: ReturnType<typeof vi.fn> }
  const ctx = { setTransform: vi.fn(), clearRect: vi.fn(), fillRect: vi.fn(), fillText: vi.fn() }
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
  vi.stubGlobal('Worker', class {
    terminate = vi.fn(); postMessage = vi.fn()
    constructor() { worker = this }
  })
  Object.defineProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen', { configurable: true, value: vi.fn(() => ({})) })
  try {
    const { container } = render(<LandingBloom />)
    const transferred = container.querySelector('canvas')
    worker.onerror!()
    expect(worker.terminate).toHaveBeenCalledOnce()
    expect(container.querySelectorAll('canvas')).toHaveLength(1)
    expect(container.querySelector('canvas')).not.toBe(transferred)
    expect(ctx.fillText).toHaveBeenCalled()
  } finally { Reflect.deleteProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen') }
})
