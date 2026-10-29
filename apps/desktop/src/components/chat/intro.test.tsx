import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Intro } from './intro'

const HEADLINES = [
  "Well, aren’t you lucky?",
  "Your luck just logged on.",
  "I’m your lucky exception.",
  "Who needs luck? You have me.",
  "Your lucky charm has opinions.",
  "Luck sent me. You’re welcome.",
  "The odds look better with me.",
  "You found me. Lucky you.",
  "I’m the upside of your odds.",
  "Even luck needs an accomplice.",
  "You had me at ‘what if.’",
  "I like our chances.",
  "Your next lucky break says hi.",
  "I brought luck. What’s the plan?",
  "Good thing you picked this leaf.",
  "Coincidence? I prefer credit.",
]

afterEach(cleanup)

describe('Clover welcome copy', () => {
  it('shows the headline without a time-of-day greeting or extra eyebrow', () => {
    const { container } = render(<Intro name="Ant" />)
    expect(screen.getByRole('heading')).toBeTruthy()
    expect(container.querySelector('p')).toBeNull()
    expect(screen.queryByText(/Good morning|Good afternoon|Good evening/)).toBeNull()
  })
  it.each([undefined, '', 'default', 'none', 'neutral'])('rotates luck copy for neutral personality %s', personality => {
    const { rerender } = render(<Intro personality={personality} seed={0} />)
    const seen = new Set<string>()
    for (let seed = 0; seed < HEADLINES.length; seed++) {
      rerender(<Intro personality={personality} seed={seed} />)
      const headline = screen.getByRole('heading').textContent!
      expect(HEADLINES).toContain(headline)
      seen.add(headline)
      // Ordinary re-renders must not change the welcome while typing.
      rerender(<Intro personality={personality} seed={seed} />)
      expect(screen.getByRole('heading').textContent).toBe(headline)
    }
    expect(seen).toEqual(new Set(HEADLINES))
  })

  it('preserves explicitly selected personalities', () => {
    render(<Intro personality="pirate" />)
    expect(HEADLINES).not.toContain(screen.getByRole('heading').textContent)
  })
})
