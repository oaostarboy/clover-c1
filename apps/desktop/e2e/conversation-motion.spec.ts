import fs from 'node:fs'
import path from 'node:path'
import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

const root = process.env.MOTION_PROOF!
// Real Electron and private gateway. Only inference is an authored local mock.
test('bounded landing to answer motion recording', async () => {
  test.setTimeout(100_000)
  fs.mkdirSync(path.join(root, 'frames'), { recursive: true })
  const fixture = await setupMockBackend({ mockServer: { holdFirstCompletionContaining: 'MOTION_REVIEW' } })
  const { page } = fixture
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  try {
    await waitForAppReady(fixture, 60_000)
    const width = Number(process.env.MOTION_VIEWPORT_WIDTH || 1280)
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.waitForTimeout(500)
    await page.screenshot({ path: path.join(root, 'landing.png') })
    const session = await page.context().newCDPSession(page)
    let frames = 0
    const frameTimes: number[] = []
    session.on('Page.screencastFrame', async e => {
      await session.send('Page.screencastFrameAck', { sessionId: e.sessionId }).catch(() => {})
      if (frames >= 120) return
      fs.writeFileSync(path.join(root, 'frames', `${String(frames++).padStart(4, '0')}.jpg`), Buffer.from(e.data, 'base64'))
      if (e.metadata.timestamp !== undefined) frameTimes.push(e.metadata.timestamp)
    })
    await session.send('Page.startScreencast', { format: 'jpeg', quality: 70, maxWidth: 1280, maxHeight: 900, everyNthFrame: 4 })
    await page.evaluate(() => {
      const data = { samples: [] as any[], marks: [] as any[], raf: [] as number[], long: [] as number[] }
      ;(window as any).__motion = data
      const start = performance.now()
      let previous = start, sample = start
      new PerformanceObserver(entries => entries.getEntries().forEach(e => data.long.push(e.duration))).observe({ entryTypes: ['longtask'] })
      const tick = (now: number) => {
        data.raf.push(now - previous); previous = now
        if (now - sample >= 40) {
          sample = now
          const rect = (selector: string) => {
            const e = document.querySelector(selector); if (!e) return null
            const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }
          }
          data.samples.push({ t: now-start, prompt: rect('.composer-human-message'), answer: rect('[data-slot="aui_assistant-message-content"]'), activity: rect('.clover-activity'), composer: rect('[contenteditable="true"]'), scroll: document.querySelector('[data-slot="aui_thread-viewport"]')?.scrollTop })
        }
        if (now - start < 11000) requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    const mark = async (name: string) => {
      await page.evaluate(name => (window as any).__motion.marks.push({ name, t: performance.now() }), name)
      await page.screenshot({ path: path.join(root, `${name}.png`) })
    }
    const composer = page.locator('[contenteditable="true"]').first()
    await composer.fill('MOTION_REVIEW. Keep the approved landing and particle artwork. Make the conversation feel like a continuation, with readable paragraphs and calm transitions.')
    await composer.press('Enter')
    await mark('sent')
    await fixture.mock.waitForHeldCompletion()
    await page.waitForTimeout(1600)
    await mark('waiting')
    fixture.mock.releaseHeldStream()
    await expect(page.locator('[data-editorial-thread]').getByText(/full boot chain is working/).first()).toBeVisible({ timeout: 30_000 })
    await mark('first-answer')
    await page.waitForTimeout(1100)
    await mark('complete')
    await composer.fill('Private fixture draft stays here')
    await page.waitForTimeout(200)
    await session.send('Page.stopScreencast')
    const data = await page.evaluate(() => ({ ...(window as any).__motion, focused: document.activeElement?.getAttribute('contenteditable') }))
    fs.writeFileSync(path.join(root, 'sequence.json'), JSON.stringify({ ...data, frames, frameTimes, errors, inference: 'authored local mock, not live model', electron: true }, null, 2))
    expect(errors).toEqual([])
    await expect(composer).toHaveText('Private fixture draft stays here')
  } finally { fixture.mock.releaseHeldStream(); await fixture.cleanup() }
})
