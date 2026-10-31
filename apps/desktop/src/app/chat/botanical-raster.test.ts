import { afterEach, expect, it, vi } from 'vitest'

import { drawBotanical } from './botanical-surface'
afterEach(() => vi.unstubAllGlobals())
it('reuses glyph coverage and a bounded pixel buffer instead of rerasterizing thousands of glyphs every frame', () => {
  const maskText = vi.fn()
  vi.stubGlobal('OffscreenCanvas', class {
    width: number; height: number
    constructor(w: number,h: number) {this.width=w;this.height=h}
    getContext() {return {setTransform(){},clearRect(){},fillText:maskText,getImageData:()=>({data: new Uint8ClampedArray(this.width*this.height*4).fill(255)})}}
  })
  const ctx = {
    canvas:{width:720,height:600},getTransform:()=>({a:1,b:0,c:0,d:1,e:0,f:0}),
    clearRect:vi.fn(),fillRect:vi.fn(),fillText:vi.fn(),
    createImageData:vi.fn((w:number,h:number)=>({width:w,height:h,data:new Uint8ClampedArray(w*h*4)})),putImageData:vi.fn()
  }
  drawBotanical(ctx as unknown as CanvasRenderingContext2D,720,600,0)
  expect(ctx.fillText).not.toHaveBeenCalled()
  expect(maskText.mock.calls.length).toBeGreaterThan(0)
  expect(maskText.mock.calls.length).toBeLessThanOrEqual(52)
  maskText.mockClear();ctx.putImageData.mockClear()
  drawBotanical(ctx as unknown as CanvasRenderingContext2D,720,600,1)
  expect(maskText).not.toHaveBeenCalled()
  expect(ctx.fillText).not.toHaveBeenCalled()
  expect(ctx.createImageData).toHaveBeenCalledOnce()
  expect(ctx.putImageData).toHaveBeenCalledOnce()
})
