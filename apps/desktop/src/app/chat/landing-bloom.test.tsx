import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LandingBloom, drawBotanical } from './landing-bloom'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('landing botanical canvas', () => {
  it('draws dense shaded glyphs and changes its geometry over time', () => {
    const glyphs: string[] = []
    const ctx = { clearRect: vi.fn(), fillRect: vi.fn(), fillText: (c: string, x: number, y: number) => glyphs.push(`${c}:${x}:${y}`) } as unknown as CanvasRenderingContext2D
    drawBotanical(ctx, 1440, 900, 0)
    const first = glyphs.join(',')
    expect(glyphs.length).toBeGreaterThan(1000)
    expect(new Set(glyphs.map(g => g[0])).size).toBeGreaterThan(3)
    glyphs.length = 0
    drawBotanical(ctx, 1440, 900, 3)
    expect(glyphs.join(',')).not.toBe(first)
  })

  it('reuses static leaf normal calculations on later frames', async () => {
    vi.resetModules()
    const { drawBotanical } = await import('./botanical-surface')
    const ctx = { clearRect: vi.fn(), fillRect: vi.fn(), fillText: vi.fn() } as unknown as CanvasRenderingContext2D
    const hypot = vi.spyOn(Math, 'hypot')
    drawBotanical(ctx, 1440, 900, 0)
    const first = hypot.mock.calls.length
    hypot.mockClear()
    drawBotanical(ctx, 1440, 900, 0.05)
    expect(hypot.mock.calls.length).toBeLessThan(first / 2)
  })

  it('does not schedule frames under reduced motion and cleans up listeners', () => {
    const ctx = { clearRect: vi.fn(), fillRect: vi.fn(), fillText: vi.fn(), setTransform: vi.fn() }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
    const media = { matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }
    vi.stubGlobal('matchMedia', () => media)
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    const { container, unmount } = render(<LandingBloom />)
    expect(container.querySelector('canvas')?.getAttribute('aria-hidden')).toBe('true')
    expect(raf).not.toHaveBeenCalled()
    expect(ctx.clearRect).toHaveBeenCalled()
    unmount()
    expect(media.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function))
  })

  it('renders persistent canvas host and veil element, toggling data-frosted', () => {
    const { container, rerender } = render(<LandingBloom frosted={false} />)
    const bloom = container.querySelector('[data-landing-bloom]')
    expect(bloom).toBeTruthy()
    expect(bloom?.getAttribute('data-frosted')).toBeNull()
    const veil = container.querySelector('[data-slot="landing-bloom-veil"]')
    expect(veil).toBeTruthy()

    rerender(<LandingBloom frosted={true} />)
    expect(bloom?.getAttribute('data-frosted')).toBe('true')
    expect(container.querySelector('[data-slot="landing-bloom-veil"]')).toBe(veil)
  })
})
