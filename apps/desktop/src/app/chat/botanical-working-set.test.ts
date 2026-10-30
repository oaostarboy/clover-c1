import { afterEach, describe, expect, it, vi } from 'vitest'

import { drawBotanical } from './botanical-surface'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('botanical frame working set', () => {
  it('does not allocate new depth or tone buffers for a same-size frame', () => {
    const ctx = { clearRect() {}, fillRect() {}, fillText() {} } as unknown as CanvasRenderingContext2D
    const Native = Float32Array
    let allocations = 0
    vi.stubGlobal('Float32Array', new Proxy(Native, {
      construct(target, args) { allocations++;

 return Reflect.construct(target, args) }
    }))
    drawBotanical(ctx, 320, 240, 0)
    expect(allocations).toBeGreaterThan(0)
    allocations = 0
    drawBotanical(ctx, 320, 240, 0.05)
    expect(allocations).toBe(0)
  })

  it('does not reuse another context or an incompatible viewport working set', () => {
    const Native = Float32Array
    let allocations = 0
    vi.stubGlobal('Float32Array', new Proxy(Native, {
      construct(target, args) { allocations++;

 return Reflect.construct(target, args) }
    }))
    const make = () => ({ clearRect() {}, fillRect() {}, fillText() {} }) as unknown as CanvasRenderingContext2D
    const a = make(), b = make()
    drawBotanical(a, 320, 240, 0)
    allocations = 0
    drawBotanical(b, 320, 240, 0)
    expect(allocations).toBeGreaterThan(0)
    allocations = 0
    drawBotanical(a, 321, 240, 0)
    expect(allocations).toBeGreaterThan(0)
  })
})
