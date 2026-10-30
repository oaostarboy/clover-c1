type Context = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D
type Coverage = {
  offsets: Int32Array; xs: Int32Array; ys: Int32Array; alpha: Float64Array
  minX: number; maxX: number; minY: number; maxY: number
}
type Location = { ix: number; iy: number; base: number; fx: number; fy: number; phase: number }
type AxisCoverage = { start: number; values: Float64Array }
const FONT = '7px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
const caches = new WeakMap<Context, GlyphRaster>()

/** Same ordered, byte-rounded compositing; fixed coverage/addresses are cached. */
class GlyphRaster {
  image: ImageData
  masks = new Map<number, Coverage>()
  locations = new Map<number, Map<number, Location>>()
  washColumns = new Map<number, AxisCoverage>()
  washRows = new Map<number, AxisCoverage>()
  constructor(readonly ctx: Context, readonly dpr: number, readonly width: number, readonly height: number) {
    this.image = ctx.createImageData(width, height)
  }
  begin() { this.image.data.fill(0) }
  // Caller has already checked coverage and clipping. Keep the original order
  // of operations and every Uint8ClampedArray write (including intermediate rounding).
  blendAt(i: number, red: number, green: number, blue: number, alpha: number) {
    const pixels = this.image.data

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
  axis(value: number, half: number, limit: number, cache: Map<number, AxisCoverage>) {
    let coverage = cache.get(value)

    if (!coverage) {
      const lo = (value - half) * this.dpr, hi = (value + half) * this.dpr
      const start = Math.max(0, Math.floor(lo)), end = Math.min(limit, Math.ceil(hi))
      const values = new Float64Array(Math.max(0, end - start))

      for (let i = 0; i < values.length; i++) {
        const p = start + i
        values[i] = Math.max(0, Math.min(p + 1, hi) - Math.max(p, lo))
      }

      coverage = { start, values }; cache.set(value, coverage)
    }

    return coverage
  }
  wash(x: number, y: number, alpha: number) {
    if (alpha <= 0) {return}
    const xc = this.axis(x, 4.5 * .46, this.width, this.washColumns)
    const yc = this.axis(y, 7 * .46, this.height, this.washRows)

    for (let row = 0; row < yc.values.length; row++) {
      let offset = ((yc.start + row) * this.width + xc.start) * 4
      const yCoverage = yc.values[row]

      for (let col = 0; col < xc.values.length; col++, offset += 4) {
        const a = alpha * .7 * xc.values[col] * yCoverage

        if (a > 0) {this.blendAt(offset, 110, 190, 155, a)}
      }
    }
  }
  glyph(glyph: string, x: number, y: number, red: number, green: number, blue: number, alpha: number) {
    if (alpha <= 0) {return}
    let column = this.locations.get(x)

    if (!column) { column = new Map(); this.locations.set(x, column) }
    let location = column.get(y)

    if (!location) {
      const dx = x * this.dpr, dy = y * this.dpr
      const ix = Math.floor(dx), iy = Math.floor(dy)
      const phaseX = Math.round((dx - ix) * 8), phaseY = Math.round((dy - iy) * 8)
      location = { ix, iy, base: (iy * this.width + ix) * 4, fx: phaseX / 8, fy: phaseY / 8, phase: phaseX * 9 + phaseY }
      column.set(y, location)
    }

    const { ix, iy, base, fx, fy, phase } = location
    const key = glyph.charCodeAt(0) * 81 + phase
    let mask = this.masks.get(key)

    if (!mask) {
      const pad = Math.ceil(8 * this.dpr), size = pad * 2 + 2
      const canvas = new OffscreenCanvas(size, size)
      const context = canvas.getContext('2d', { willReadFrequently: true })

      if (!context) {throw new Error('Glyph coverage canvas unavailable')}
      context.setTransform(this.dpr, 0, 0, this.dpr, 0, 0)
      context.font = FONT; context.textAlign = 'center'; context.textBaseline = 'middle'; context.fillStyle = '#fff'
      context.fillText(glyph, (pad + fx) / this.dpr, (pad + fy) / this.dpr)
      const pixels = context.getImageData(0, 0, size, size).data
      const xs: number[] = [], ys: number[] = [], offsets: number[] = [], alphas: number[] = []
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity

      for (let y = 0; y < size; y++) {for (let x = 0; x < size; x++) {
        const a = pixels[(y * size + x) * 4 + 3]

        if (a) {
          const px = x - pad, py = y - pad
          xs.push(px); ys.push(py); offsets.push((py * this.width + px) * 4); alphas.push(a / 255)
          minX = Math.min(minX, px); maxX = Math.max(maxX, px); minY = Math.min(minY, py); maxY = Math.max(maxY, py)
        }
      }}

      mask = { xs: Int32Array.from(xs), ys: Int32Array.from(ys), offsets: Int32Array.from(offsets), alpha: Float64Array.from(alphas), minX, maxX, minY, maxY }
      this.masks.set(key, mask)
    }

    const { offsets, alpha: coverage } = mask

    if (ix + mask.minX >= 0 && iy + mask.minY >= 0 && ix + mask.maxX < this.width && iy + mask.maxY < this.height) {
      for (let i = 0; i < offsets.length; i++) {this.blendAt(base + offsets[i], red, green, blue, alpha * coverage[i])}
    } else {
      for (let i = 0; i < offsets.length; i++) {
        const px = ix + mask.xs[i], py = iy + mask.ys[i]

        if (px >= 0 && py >= 0 && px < this.width && py < this.height) {this.blendAt(base + offsets[i], red, green, blue, alpha * coverage[i])}
      }
    }
  }
  finish() { this.ctx.putImageData(this.image, 0, 0) }
}

export function botanicalRaster(ctx: Context): GlyphRaster | null {
  if (typeof OffscreenCanvas === 'undefined' || !ctx.getTransform || !ctx.createImageData || !ctx.putImageData) {return null}
  const t = ctx.getTransform()

  if (t.b || t.c || t.e || t.f || t.a !== t.d || t.a <= 0) {return null}
  const { width, height } = ctx.canvas
  let cache = caches.get(ctx)

  if (!cache || cache.width !== width || cache.height !== height || cache.dpr !== t.a) {
    cache = new GlyphRaster(ctx, t.a, width, height)
    caches.set(ctx, cache)
  }

  cache.begin()

  return cache
}
