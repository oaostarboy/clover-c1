type Context = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D
type Coverage = { x: number; y: number; alpha: number }[]
const FONT = '7px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
const caches = new WeakMap<Context, GlyphRaster>()

/** Coverage is rasterized once per glyph/subpixel phase, never once per cell.
 * The pixel buffer composites the same ordered glyphs and the real frost wash.
 * One putImageData replaces thousands of Canvas text/display-list operations.
 */
class GlyphRaster {
  image: ImageData
  masks = new Map<string, Coverage>()
  constructor(readonly ctx: Context, readonly dpr: number, readonly width: number, readonly height: number) {
    this.image = ctx.createImageData(width, height)
  }
  begin() { this.image.data.fill(0) }
  blend(x: number, y: number, red: number, green: number, blue: number, alpha: number) {
    if (x < 0 || y < 0 || x >= this.width || y >= this.height || alpha <= 0) return
    const pixels = this.image.data, i = (y * this.width + x) * 4
    if (!pixels[i + 3]) {
      pixels[i] = red; pixels[i + 1] = green; pixels[i + 2] = blue; pixels[i + 3] = alpha * 255
      return
    }
    const old = pixels[i + 3] / 255, out = alpha + old * (1 - alpha)
    const remaining = old * (1 - alpha)
    pixels[i] = (red * alpha + pixels[i] * remaining) / out
    pixels[i + 1] = (green * alpha + pixels[i + 1] * remaining) / out
    pixels[i + 2] = (blue * alpha + pixels[i + 2] * remaining) / out
    pixels[i + 3] = out * 255
  }
  wash(x: number, y: number, alpha: number) {
    const left = (x - 4.5 * .46) * this.dpr, right = (x + 4.5 * .46) * this.dpr
    const top = (y - 7 * .46) * this.dpr, bottom = (y + 7 * .46) * this.dpr
    for (let py = Math.floor(top); py < Math.ceil(bottom); py++) {
      const yCoverage = Math.max(0, Math.min(py + 1, bottom) - Math.max(py, top))
      for (let px = Math.floor(left); px < Math.ceil(right); px++) {
        const xCoverage = Math.max(0, Math.min(px + 1, right) - Math.max(px, left))
        this.blend(px, py, 110, 190, 155, alpha * .7 * xCoverage * yCoverage)
      }
    }
  }
  glyph(glyph: string, x: number, y: number, red: number, green: number, blue: number, alpha: number) {
    const dx = x * this.dpr, dy = y * this.dpr
    const ix = Math.floor(dx), iy = Math.floor(dy)
    // Fixed 1/8-pixel phases bound cache work even at unusual native zooms.
    // The standard 1x, 1.25x and 2x grids fall exactly on these phases.
    const fx = Math.round((dx - ix) * 8) / 8, fy = Math.round((dy - iy) * 8) / 8
    const key = `${glyph}:${fx}:${fy}`
    let mask = this.masks.get(key)
    if (!mask) {
      const pad = Math.ceil(8 * this.dpr), size = pad * 2 + 2
      const canvas = new OffscreenCanvas(size, size)
      const context = canvas.getContext('2d', { willReadFrequently: true })
      if (!context) throw new Error('Glyph coverage canvas unavailable')
      context.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
      context.font = FONT; context.textAlign = 'center'; context.textBaseline = 'middle'; context.fillStyle = '#fff'
      context.fillText(glyph, (pad + fx) / this.dpr, (pad + fy) / this.dpr)
      const pixels = context.getImageData(0, 0, size, size).data
      mask = []
      for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
        const a = pixels[(y * size + x) * 4 + 3]
        if (a) mask.push({ x: x - pad, y: y - pad, alpha: a / 255 })
      }
      this.masks.set(key, mask)
    }
    for (const pixel of mask) this.blend(ix + pixel.x, iy + pixel.y, red, green, blue, alpha * pixel.alpha)
  }
  finish() { this.ctx.putImageData(this.image, 0, 0) }
}

export function botanicalRaster(ctx: Context): GlyphRaster | null {
  if (typeof OffscreenCanvas === 'undefined' || !ctx.getTransform || !ctx.createImageData || !ctx.putImageData) return null
  const t = ctx.getTransform()
  if (t.b || t.c || t.e || t.f || t.a !== t.d || t.a <= 0) return null
  const { width, height } = ctx.canvas
  let cache = caches.get(ctx)
  if (!cache || cache.width !== width || cache.height !== height || cache.dpr !== t.a) {
    cache = new GlyphRaster(ctx, t.a, width, height)
    caches.set(ctx, cache)
  }
  cache.begin()
  return cache
}
