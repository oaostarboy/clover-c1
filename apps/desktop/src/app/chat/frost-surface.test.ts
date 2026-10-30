import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const liquidGlassCss = readFileSync('src/app/chat/liquid-glass.css', 'utf8')
const landingCss = readFileSync('src/app/chat/landing.css', 'utf8')

describe('frosted composer surface', () => {
  it('uses a bounded blur without a distortion filter or chrome highlights', () => {
    expect(liquidGlassCss).toMatch(/backdrop-filter:\s*blur\(20px\) saturate\(115%\)/)
    expect(liquidGlassCss).not.toMatch(/clover-glass-refraction|rgb\(255 255 255 \/ 0\.4\)/)
  })
})

describe('frosted background transition on first message', () => {
  it('defines an unhurried cubic-bezier transition on veil backdrop-filter, background-color, and opacity', () => {
    expect(landingCss).toMatch(
      /transition:[\s\S]*?backdrop-filter\s+500ms\s+cubic-bezier\(0\.22,\s*1,\s*0\.36,\s*1\)[\s\S]*?background-color\s+500ms\s+cubic-bezier\(0\.22,\s*1,\s*0\.36,\s*1\)[\s\S]*?opacity\s+500ms\s+cubic-bezier\(0\.22,\s*1,\s*0\.36,\s*1\)/
    )
  })

  it('keeps the ASCII clover background crisp with no veil on the initial landing state', () => {
    expect(landingCss).toMatch(
      /\[data-chat-surface\]\[data-landing\]\s+\[data-landing-bloom\][\s\S]*?opacity:\s*1;[\s\S]*?filter:\s*none;/
    )
    expect(landingCss).toMatch(
      /\[data-chat-surface\]\[data-landing\]\s+\[data-slot='landing-bloom-veil'\][\s\S]*?opacity:\s*0;/
    )
  })

  it('animates into a much more frosted glass look with strong blur and softened ASCII contrast during active chat', () => {
    expect(landingCss).toMatch(
      /\[data-chat-surface\]:not\(\[data-landing\]\)[\s\S]*?opacity:\s*0\.16;/
    )
    expect(landingCss).toMatch(
      /\[data-chat-surface\]:not\(\[data-landing\]\)[\s\S]*?filter:\s*blur\(1\.5px\);/
    )
    expect(landingCss).toMatch(
      /\[data-chat-surface\]:not\(\[data-landing\]\)[\s\S]*?backdrop-filter:\s*blur\(16px\)\s*saturate\(130%\);/
    )
    expect(landingCss).toMatch(
      /background-color:\s*color-mix\(in\s+srgb,\s*var\(--ui-chat-surface-background\)\s+78%,\s*transparent\);/
    )
  })

  it('disables transitions under prefers-reduced-motion', () => {
    expect(landingCss).toMatch(
      /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)[\s\S]*?transition:\s*none\s*!important;/
    )
  })
})

