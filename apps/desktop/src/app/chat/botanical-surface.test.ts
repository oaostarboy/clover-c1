import { createHash } from 'node:crypto'

import { describe, expect, it, vi } from 'vitest'

import { drawBotanical } from './botanical-surface'

describe('botanical visible-surface work', () => {
  it('preserves the baseline ordered fallback operations across contexts and resizes', () => {
    const calls: unknown[][] = []

    const make = () => {
      const context = {
        fillStyle: '',
        clearRect(...args: number[]) { calls.push(['clear', ...args]) },
        fillRect(...args: number[]) { calls.push(['rect', context.fillStyle, ...args]) },
        fillText(text: string, x: number, y: number) { calls.push(['text', context.fillStyle, text, x, y]) },
      }

      return context as unknown as CanvasRenderingContext2D
    }

    const contexts = [make(), make()]
    const cases = [[0, 320, 240, 0], [0, 320, 240, 0.5], [0, 321, 241, 5], [1, 320, 240, 0], [1, 0, 240, 1], [0, 320, 0, 2], [0, 320, 240, 0]]

    const actual = cases.map(([ctx, width, height, time]) => {
      calls.length = 0
      drawBotanical(contexts[ctx], width, height, time)

      return createHash('sha256').update(JSON.stringify(calls)).digest('hex')
    })

    // SHA-256 of the complete ordered JSON operations captured from baseline 95b7916f.
    expect(actual).toEqual([
      'b706e12c95267cb2837c855f11f7ffd87d729a97eaa68f54425363429bca02ff',
      '983955b37a9dda5a714e4ad3237f5bbc9e5fc9bbf87568cce41ea032f2f77bad',
      '54f8c6b0a8052135967046ac8060843dbc98050172866395e867e887eee5bb4c',
      'b706e12c95267cb2837c855f11f7ffd87d729a97eaa68f54425363429bca02ff',
      '47c7aa6b4c7a71a10b85d22314c584cc781a252dc4ea6c67b1ebf6e3e39cec02',
      '71d6c01004e485364bb6c07c30e2ead49c4b0f092fd0c8be2a7cce7ce893c2a8',
      'b706e12c95267cb2837c855f11f7ffd87d729a97eaa68f54425363429bca02ff',
    ])
  })
  it('does not recompute cell hypot or jitter sine on a warm frame', () => {
    const context = { clearRect() {}, fillRect() {}, fillText() {} } as unknown as CanvasRenderingContext2D
    drawBotanical(context, 320, 240, 0)
    const hypot = vi.spyOn(Math, 'hypot')
    const sin = vi.spyOn(Math, 'sin')

    try {
      drawBotanical(context, 320, 240, 0.25)
      expect(hypot).not.toHaveBeenCalled()
      const cellInputs = new Set<number>()

      for (let row = 0; row < Math.ceil(240 / 7); row++) {
        for (let col = 0; col < Math.ceil(320 / 4.5); col++) {
          if (col || row) { cellInputs.add(col * 127.1 + row * 311.7) }
        }
      }

      expect(sin.mock.calls.filter(([value]) => cellInputs.has(value))).toHaveLength(0)
    } finally {
      hypot.mockRestore()
      sin.mockRestore()
    }
  })
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
