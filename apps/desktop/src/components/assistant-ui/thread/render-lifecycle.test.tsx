import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

import { ActivityPresence } from './activity-presence'
import { CloverParticles } from './clover-particles'

describe('render lifecycle: rapid-completion, reconnect, replay, keyboard', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      scale: vi.fn(),
      clearRect: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
      translate: vi.fn()
    } as unknown as CanvasRenderingContext2D)

    vi.stubGlobal('matchMedia', () => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn()
    }))
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('handles rapid completion gracefully without dangling timers or errors', () => {
    function RapidTurn() {
      const [active, setActive] = useState(true)
      return (
        <div>
          <button onClick={() => setActive(false)}>Finish</button>
          <ActivityPresence active={active} phase="working">
            <span>Clover is working</span>
          </ActivityPresence>
        </div>
      )
    }

    const { container } = render(<RapidTurn />)
    expect(screen.getByText('Clover is working')).toBeTruthy()
    expect(container.querySelector('.clover-activity-art')).toBeTruthy()

    // Rapid completion occurs 50ms into turn
    act(() => vi.advanceTimersByTime(50))
    const btn = screen.getByRole('button', { name: 'Finish' })
    act(() => btn.click())

    // Accessible text unmounts immediately
    expect(screen.queryByText('Clover is working')).toBeNull()
    // Departing art is held during graceful exit
    expect(container.querySelector('.clover-activity-art')?.getAttribute('data-state')).toBe('leaving')

    // Exit finishes and unmounts
    act(() => vi.advanceTimersByTime(250))
    expect(container.querySelector('.clover-activity-art')).toBeNull()
  })

  it('does not mount particle animation loop for replayed or settled historical messages', () => {
    // For settled messages, active is false from mount
    const { container } = render(
      <div>
        <p>Replayed historical answer text</p>
        <ActivityPresence active={false} phase="working">
          <span>Should not render</span>
        </ActivityPresence>
      </div>
    )

    expect(screen.getByText('Replayed historical answer text')).toBeTruthy()
    expect(screen.queryByText('Should not render')).toBeNull()
    expect(container.querySelector('canvas')).toBeNull()
    expect(container.querySelector('.clover-activity-art')).toBeNull()
  })

  it('cleans up all resources on unmount during active execution (e.g. reconnect or session switch)', () => {
    const { container, unmount } = render(
      <ActivityPresence active={true} phase="working">
        <span>Active work</span>
      </ActivityPresence>
    )

    expect(container.querySelector('canvas')).toBeTruthy()
    unmount()
    // Advancing timers post-unmount should not throw or cause setState warnings
    expect(() => act(() => vi.advanceTimersByTime(1000))).not.toThrow()
  })

  it('keeps keyboard focus in input elements during state transitions', () => {
    function InteractiveSurface() {
      const [active, setActive] = useState(true)
      return (
        <div>
          <input data-testid="chat-input" defaultValue="User query" />
          <button data-testid="toggle-turn" onClick={() => setActive(!active)}>Toggle</button>
          <ActivityPresence active={active} phase="working">
            <span>Processing</span>
          </ActivityPresence>
        </div>
      )
    }

    render(<InteractiveSurface />)
    const input = screen.getByTestId('chat-input') as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)

    // First token arrives, active flips to false
    const toggle = screen.getByTestId('toggle-turn')
    act(() => toggle.click())

    // Keyboard focus remains strictly on input
    expect(document.activeElement).toBe(input)

    // Advance through exit animation
    act(() => vi.advanceTimersByTime(300))
    expect(document.activeElement).toBe(input)
  })
})
