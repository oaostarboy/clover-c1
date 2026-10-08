import { describe, expect, it } from 'vitest'

import { contrastRatio, hexToOklch } from './color'
import { BUILTIN_THEME_LIST, BUILTIN_THEMES, cloverBlueTheme, cloverTheme, DEFAULT_SKIN_NAME } from './presets'
import { retintTheme } from './retint'

// Clover decor (mascot home, glow, watermark, glass send) is opt-in per theme.
// Ant's rule: other skins keep their own look, so exactly the clover skin
// carries it and nothing else does.
describe('Clover decor is scoped to the clover skin', () => {
  it('is carried by the default clover skin', () => {
    expect(DEFAULT_SKIN_NAME).toBe('clover')
    expect(cloverTheme.decor).toBe('clover')
  })

  it('is absent from every other built-in theme', () => {
    const decorated = BUILTIN_THEME_LIST.filter(theme => theme.decor).map(theme => theme.name)

    expect(decorated).toEqual(['clover'])
  })
})

describe('the clover amethyst accent', () => {
  const cases = [
    { appearance: 'light', colors: cloverTheme.colors },
    { appearance: 'dark', colors: cloverTheme.darkColors! }
  ] as const

  it.each(cases)('$appearance keeps the four seed slots locked together', ({ colors }) => {
    for (const key of ['ring', 'midground', 'composerRing'] as const) {
      expect(colors[key]).toBe(colors.primary)
    }
  })

  it.each(cases)('$appearance clears AA on its own sidebar', ({ colors }) => {
    expect(contrastRatio(colors.primary, colors.sidebarBackground!)).toBeGreaterThanOrEqual(4.5)
  })

  it.each(cases)('$appearance keeps text on the accent readable', ({ colors }) => {
    expect(contrastRatio(colors.primary, colors.primaryForeground)).toBeGreaterThanOrEqual(4.5)
  })

  it('is one violet at two lightnesses', () => {
    const light = hexToOklch(cloverTheme.colors.primary)!
    const dark = hexToOklch(cloverTheme.darkColors!.primary)!

    expect(Math.abs(light.h - dark.h)).toBeLessThan(3)
    expect(dark.l).toBeGreaterThan(light.l)
  })

  it('only re-seeds the accent family; the neutrals are Clover Blue’s', () => {
    for (const key of ['background', 'foreground', 'card', 'border', 'muted', 'sidebarBackground'] as const) {
      expect(cloverTheme.colors[key]).toBe(cloverBlueTheme.colors[key])
      expect(cloverTheme.darkColors![key]).toBe(cloverBlueTheme.darkColors![key])
    }
  })

  it('round-trips through retint in light mode (the dev accent picker stays exact)', () => {
    expect(retintTheme(cloverTheme, cloverTheme.colors.primary).colors).toEqual(cloverTheme.colors)
  })
})

describe('Clover Blue stays available', () => {
  it('is registered with the pre-mascot blue and no decor', () => {
    expect(BUILTIN_THEMES['clover-blue']).toBe(cloverBlueTheme)
    expect(cloverBlueTheme.colors.primary).toBe('#0053fd')
    expect(cloverBlueTheme.decor).toBeUndefined()
  })
})
