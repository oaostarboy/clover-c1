import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ActivityPresence } from './activity-presence'

describe('ActivityPresence', () => {
  let reduced = false

  beforeEach(() => {
    vi.useFakeTimers()
    reduced = false
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      scale: vi.fn(),
      clearRect: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn()
    } as unknown as CanvasRenderingContext2D)

    vi.stubGlobal('matchMedia', () => ({
      get matches() {
        return reduced
      },
      addEventListener: vi.fn(),
      removeEventListener: vi.fn()
    }))
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('renders children and active particle art when active is true', () => {
    const { container } = render(
      <ActivityPresence active={true} phase="working">
        <span>Working status</span>
      </ActivityPresence>
    )

    expect(screen.getByText('Working status')).toBeTruthy()
    const art = container.querySelector('.clover-activity-art')
    expect(art).toBeTruthy()
    expect(art?.getAttribute('data-state')).toBe('present')
    expect(container.querySelector('canvas')).toBeTruthy()
  })

  it('drops accessible children immediately and holds leaving art during departure', () => {
    const { container, rerender } = render(
      <ActivityPresence active={true} phase="working">
        <span>Working status</span>
      </ActivityPresence>
    )

    rerender(
      <ActivityPresence active={false} phase="working">
        <span>Working status</span>
      </ActivityPresence>
    )

    // Accessibility state is dropped at once
    expect(screen.queryByText('Working status')).toBeNull()

    // Visual art remains in leaving state
    const art = container.querySelector('.clover-activity-art')
    expect(art).toBeTruthy()
    expect(art?.getAttribute('data-state')).toBe('leaving')

    // After departure duration elapses, unmounts cleanly
    act(() => vi.advanceTimersByTime(250))
    expect(container.querySelector('.clover-activity-art')).toBeNull()
    expect(container.querySelector('.clover-activity-anchor')).toBeNull()
  })

  it('unmounts immediately under reduced motion when becoming inactive', () => {
    reduced = true
    const { container, rerender } = render(
      <ActivityPresence active={true} phase="working">
        <span>Working status</span>
      </ActivityPresence>
    )

    rerender(
      <ActivityPresence active={false} phase="working">
        <span>Working status</span>
      </ActivityPresence>
    )

    expect(screen.queryByText('Working status')).toBeNull()
    expect(container.querySelector('.clover-activity-art')).toBeNull()
  })
})
