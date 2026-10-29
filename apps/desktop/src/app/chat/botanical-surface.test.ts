import { describe, expect, it, vi } from 'vitest'
import { drawBotanical } from './botanical-surface'

describe('botanical visible-surface work', () => {
  it('lights only the frontmost sample in each occupied glyph cell', () => {
    let glyphs = 0
    const context = {
      clearRect() {}, fillRect() {}, fillText() { glyphs++ },
    } as unknown as CanvasRenderingContext2D
    const pow = vi.spyOn(Math, 'pow')
    try {
      drawBotanical(context, 1384, 830, 3)
      expect(glyphs).toBeGreaterThan(1000)
      // The exponent-18 highlight is evaluated once per visible leaf cell,
      // never for the dense source samples that lose the depth test.
      const highlights = pow.mock.calls.filter(([, exponent]) => exponent === 18).length
      expect(highlights).toBeLessThanOrEqual(glyphs)
    } finally { pow.mockRestore() }
  })
})
