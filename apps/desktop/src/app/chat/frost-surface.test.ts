import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/app/chat/liquid-glass.css', 'utf8')

describe('frosted composer surface', () => {
  it('uses a bounded blur without a distortion filter or chrome highlights', () => {
    expect(css).toMatch(/backdrop-filter:\s*blur\(20px\) saturate\(115%\)/)
    expect(css).not.toMatch(/clover-glass-refraction|rgb\(255 255 255 \/ 0\.4\)/)
  })
})
