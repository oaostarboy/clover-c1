import { botanicalRaster } from './botanical-raster'

// Curved leaflet surfaces, projected into a screen-aligned glyph grid.
// Geometry is cached; the per-frame pass only rotates, lights and depth-tests it.
const RAMP = ' .,:;irsx*o%#@'

const PLANTS = [
  { x: 0.12, y: 0.36, scale: 0.23, spin: -0.4, tilt: 0.42, phase: 0.3, ink: 0.8 },
  { x: 0.16, y: 0.72, scale: 0.17, spin: 0.5, tilt: -0.45, phase: 2.2, ink: 0.55 },
  { x: 0.74, y: 0.53, scale: 0.25, spin: 0.15, tilt: 0.64, phase: 1.3, ink: 0.76 },
  { x: 0.94, y: 0.66, scale: 0.24, spin: -0.7, tilt: 0.22, phase: 3.3, ink: 0.6 },
  { x: 0.50, y: 1.04, scale: 0.28, spin: 0.8, tilt: 0.55, phase: 4.6, ink: 0.7 }
]

type Sample = { u: number; r: number; z: number; du: number; dr: number; vein: number }

function height(u: number, r: number) {
  return 0.15 * Math.sin(Math.PI * r) + 0.18 * Math.sqrt(u * u + 0.003) +
    0.085 * Math.sin(u * 4 + r * 3) * Math.sin(Math.PI * r)
}

const SURFACE: Sample[] = []

for (let v = -1.02; v <= 1.24; v += 0.026) {
  for (let u = -1.15; u <= 1.15; u += 0.026) {
    const q = u * u + v * v - 1

    if (q * q * q - u * u * v * v * v > 0) {continue}
    const r = (v + 1.02) / 2.26
    const z = height(u, r)
    const du = (height(u + 0.005, r) - z) / 0.005
    const dr = (height(u, r + 0.005) - z) / 0.005
    const midrib = Math.exp(-Math.abs(u) * 40)
    const sideVein = Math.exp(-Math.abs(Math.sin(r * 26 - Math.abs(u) * 7)) * 20)
    const crescent = Math.exp(-Math.pow((r - 0.65 + Math.abs(u) * 0.13) * 22, 2))
    SURFACE.push({u, r, z, du, dr, vein: midrib * 0.17 + sideVein * 0.08 + crescent * 0.10})
  }
}

// Leaf lengths and source geometry never change across frames. Cache their
// normal lengths without quantizing, so lighting remains identical.
const normalLengths = new Map<number, Float64Array>()

function normalsForLength(length: number) {
  let normals = normalLengths.get(length)

  if (!normals) {
    normals = Float64Array.from(SURFACE, point => Math.hypot(-point.du / 0.53, -point.dr / length, 1))
    normalLengths.set(length, normals)
  }

  return normals
}

type Context = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D

// One current-size working set per canvas, not an accumulating resize cache.
const frameBuffers = new WeakMap<Context, {
  width: number; height: number; depth: Float32Array; tones: Float32Array;
  inks: Float32Array; points: Int32Array; leaves: Uint8Array; attenuation: Float64Array; jitter: Float64Array
}>()

export function drawBotanical(ctx: Context, width: number, heightPx: number, time: number) {
  const cw = 4.5, ch = 7
  const cols = Math.ceil(width / cw), rows = Math.ceil(heightPx / ch)
  let buffers = frameBuffers.get(ctx)

  if (!buffers || buffers.width !== width || buffers.height !== heightPx) {
    const size = cols * rows
    const attenuation = new Float64Array(size), jitter = new Float64Array(size)

    for (let row = 0; row < rows; row++) {for (let col = 0; col < cols; col++) {
      const i = row * cols + col
      const x = (col + 0.5) * cw, y = (row + 0.5) * ch
      const clear = Math.hypot((x / width - 0.5) / 0.33, (y / heightPx - 0.48) / 0.23)
      attenuation[i] = Math.min(1, Math.max(0.55, (clear - 0.82) * 3))
      const noise = Math.sin(col * 127.1 + row * 311.7) * 43758.5453
      jitter[i] = (noise - Math.floor(noise) - 0.5) * 0.15
    }}

    buffers = { width, height: heightPx, depth: new Float32Array(size), tones: new Float32Array(size),
      inks: new Float32Array(size), points: new Int32Array(size), leaves: new Uint8Array(size), attenuation, jitter }
    frameBuffers.set(ctx, buffers)
  }

  const { depth, tones, inks, points, leaves, attenuation, jitter } = buffers
  depth.fill(-Infinity); tones.fill(0); inks.fill(0); points.fill(-1); leaves.fill(0)
  // Keep the depth winner, then light it once. Lighting all 108k source
  // samples wastes most of the worker on hidden/overwritten fragments.
  const lighting: { co: number; so: number; c: number; s: number; ct: number; st: number; cs: number; ss: number; length: number; normals: Float64Array }[] = []
  const unit = Math.min(width, heightPx)

  const deposit = (x: number, y: number, z: number, tone: number, ink: number, point = -1, leaf = 0) => {
    const col = Math.floor(x / cw), row = Math.floor(y / ch)

    if (col < 0 || col >= cols || row < 0 || row >= rows) {return}
    const index = row * cols + col

    if (z <= depth[index]) {return}
    depth[index] = z; tones[index] = tone; inks[index] = ink
    points[index] = point; leaves[index] = leaf
  }

  for (const plant of PLANTS) {
    const size = unit * plant.scale
    const cx = width * plant.x, cy = heightPx * plant.y
    const t = time * 0.35 + plant.phase
    const spin = plant.spin + Math.sin(t) * 0.035
    const cs = Math.cos(spin), ss = Math.sin(spin)
    const ct = Math.cos(plant.tilt), st = Math.sin(plant.tilt)

    const project = (x: number, y: number, z: number) => {
      const yy = y * ct - z * st

      return [cx + size * (x * cs - yy * ss), cy + size * (x * ss + yy * cs), y * st + z * ct]
    }

    // Stalks follow a continuous curve from the same junction as the leaflets.
    for (let r = 0; r < 2.8; r += 0.018) {
      const x = 0.12 * Math.sin(r * 1.7 + plant.phase) * r
      const p = project(x, r, -0.1)
      deposit(p[0], p[1], -2, 0.34 + 0.10 * Math.sin(r * 2), plant.ink * 0.85)
    }

    for (let leaf = 0; leaf < 4; leaf++) {
      const a = spin * 0.1 + leaf * Math.PI / 2 + Math.PI / 4 + 0.07 * Math.sin(plant.phase + leaf * 2)
      const c = Math.cos(a), s = Math.sin(a)
      // A travelling flex changes the actual leaf normals and foreshortening.
      const opening = 0.24 + 0.20 * Math.sin(t + leaf * 0.55)
      const co = Math.cos(opening), so = Math.sin(opening)
      const length = 1 + 0.10 * Math.sin(plant.phase + leaf * 3)
      const normals = normalsForLength(length)
      const lightingIndex = lighting.length
      lighting.push({ co, so, c, s, ct, st, cs, ss, length, normals })

      for (let pointIndex = 0; pointIndex < SURFACE.length; pointIndex++) {
        const point = SURFACE[pointIndex]
        const lateral = point.u * 0.53
        const radial = point.r * length
        const rr = radial * co - point.z * so
        const zz = radial * so + point.z * co
        const x = rr * c - lateral * s, y = rr * s + lateral * c
        const yy = y * ct - zz * st
        const px = cx + size * (x * cs - yy * ss), py = cy + size * (x * ss + yy * cs)
        const z = y * st + zz * ct
        const col = Math.floor(px / cw), row = Math.floor(py / ch)

        if (col < 0 || col >= cols || row < 0 || row >= rows) {continue}
        const index = row * cols + col

        if (z <= depth[index]) {continue}
        depth[index] = z; tones[index] = 0; inks[index] = plant.ink
        points[index] = pointIndex; leaves[index] = lightingIndex
      }
    }
  }

  const raster = botanicalRaster(ctx)

  if (!raster) {ctx.clearRect(0, 0, width, heightPx)}
  ctx.font = '7px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle'

  for (let row = 0; row < rows; row++) {for (let col = 0; col < cols; col++) {
    const i = row * cols + col

    if (!inks[i]) {continue}
    const x = (col + 0.5) * cw, y = (row + 0.5) * ch
    const alpha = inks[i] * attenuation[i]

    if (alpha < 0.03) {continue}
    let tone = tones[i]

    if (points[i] >= 0) {
      const pointIndex = points[i], point = SURFACE[pointIndex]
      const { co, so, c, s, ct, st, cs, ss, length, normals } = lighting[leaves[i]]
      let nx = -point.du / 0.53, ny = -point.dr / length, nz = 1
      const nlen = normals[pointIndex]
      nx /= nlen; ny /= nlen; nz /= nlen
      const nr = ny * co - nz * so, nn = ny * so + nz * co
      const wx = nr * c - nx * s, wy = nr * s + nx * c
      const py = wy * ct - nn * st, pz = wy * st + nn * ct
      const rx = wx * cs - py * ss, ry = wx * ss + py * cs
      const diffuse = Math.max(0, rx * -0.44 + ry * -0.55 + pz * 0.71)
      const specular = Math.pow(Math.max(0, pz * 0.94 - ry * 0.34), 18) * 0.16
      // Match the previous Float32 tone-buffer rounding exactly.
      tone = Math.fround(Math.min(1, 0.09 + diffuse * 0.65 + specular + point.vein))
    }

    const index = Math.max(1, Math.min(RAMP.length - 1, Math.floor((tone + jitter[i]) * RAMP.length)))
    const red = Math.round(45 + tone * 135), green = Math.round(80 + tone * 149), blue = Math.round(72 + tone * 145)

    // The glyphs alone occupy only a fraction of each cell. Once blurred they
    // average almost to the dark canvas, so the glass looks opaque even when it
    // transmits correctly. A quiet cell wash follows the actual leaf geometry.
    if (inks[i] > 0.75 && inks[i] < 0.78) {
      if (raster) {raster.wash(x, y, alpha)}
      else {
        ctx.fillStyle = `rgba(110, 190, 155, ${alpha * 0.7})`
        ctx.fillRect(x - cw * 0.46, y - ch * 0.46, cw * 0.92, ch * 0.92)
      }
    }

    if (raster) {raster.glyph(RAMP[index], x, y, red, green, blue, alpha)}
    else {
      ctx.fillStyle = `rgba(${red},${green},${blue},${alpha})`
      ctx.fillText(RAMP[index], x, y)
    }
  }}

  raster?.finish()
}
