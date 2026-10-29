import { drawBotanical } from './botanical-surface'

let canvas: OffscreenCanvas
let ctx: OffscreenCanvasRenderingContext2D
let width = 1, height = 1, dpr = 1, elapsed = 0, last = 0
let active = false
// Exposed in the worker for integration diagnostics, not sent to the UI thread.
;(self as typeof self & { __landingBloomFrames: number }).__landingBloomFrames = 0
let timer: ReturnType<typeof setTimeout> | undefined

function draw() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  drawBotanical(ctx, width, height, elapsed / 1000)
  ;(self as typeof self & { __landingBloomFrames: number }).__landingBloomFrames++
}
function tick() {
  if (!active) return
  const start = performance.now()
  if (last) elapsed += start - last
  last = start
  draw()
  timer = setTimeout(tick, Math.max(0, 1000 / 60 - (performance.now() - start)))
}
self.onmessage = (event: MessageEvent) => {
  const data = event.data
  if (data.type === 'init') {
    canvas = data.canvas
    const context = canvas.getContext('2d')
    if (!context) throw new Error('OffscreenCanvas 2D context unavailable')
    ctx = context
  } else if (data.type === 'resize') {
    width = data.width; height = data.height; dpr = data.dpr
    canvas.width = Math.max(1, Math.round(width * dpr))
    canvas.height = Math.max(1, Math.round(height * dpr))
    if (!active) draw()
  } else if (data.type === 'active') {
    clearTimeout(timer)
    active = data.active
    last = 0
    if (data.reduced) { elapsed = 0; draw() }
    if (active) tick()
  }
}
