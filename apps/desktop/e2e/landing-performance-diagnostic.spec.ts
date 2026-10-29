import { writeFileSync } from 'node:fs'
import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

test('measure landing animation main-thread cost against static baseline', async () => {
  const fixture = await setupMockBackend()
  try {
    await waitForAppReady(fixture, 120_000)
    const { page, app } = fixture
    await expect.poll(() => page.workers().filter(w => w.url().includes('landing-bloom.worker')).length).toBe(1)
    const bloomWorker = page.workers().find(w => w.url().includes('landing-bloom.worker'))!
    const frameCount = () => bloomWorker.evaluate(() => (globalThis as typeof globalThis & { __landingBloomFrames: number }).__landingBloomFrames)
    await page.setViewportSize({ width: 1384, height: 830 })
    const gpu = await app.evaluate(({ app }) => ({ features: app.getGPUFeatureStatus(), hardware: app.isHardwareAccelerationEnabled() }))
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Performance.enable')
    const results: unknown[] = []
    for (const phase of ['static', 'animated', 'animated-no-blur', 'static-again']) {
      await page.emulateMedia({ reducedMotion: phase.startsWith('static') ? 'reduce' : 'no-preference' })
      await page.locator('[data-slot="composer-surface"]').evaluate((el, off) => {
        if (off) (el as HTMLElement).style.backdropFilter = 'none'
        else (el as HTMLElement).style.removeProperty('backdrop-filter')
      }, phase === 'animated-no-blur')
      await page.waitForTimeout(500)
      const before = await cdp.send('Performance.getMetrics')
      const beforeFrames = await frameCount()
      const frames = await page.evaluate(() => new Promise<{count:number; median:number; p95:number; max:number; longTasks:number; longTaskMs:number}>(resolve => {
        const intervals: number[] = []; const tasks: number[] = []
        const observer = new PerformanceObserver(list => tasks.push(...list.getEntries().map(e => e.duration)))
        observer.observe({ entryTypes: ['longtask'] })
        const start = performance.now(); let previous = start
        const tick = (now: number) => {
          intervals.push(now - previous); previous = now
          if (now - start < 4000) { requestAnimationFrame(tick); return }
          observer.disconnect(); intervals.sort((a,b) => a-b)
          resolve({count:intervals.length, median:intervals[Math.floor(intervals.length*.5)],p95:intervals[Math.floor(intervals.length*.95)],max:intervals[intervals.length-1],longTasks:tasks.length,longTaskMs:tasks.reduce((a,b)=>a+b,0)})
        }
        requestAnimationFrame(tick)
      }))
      const after = await cdp.send('Performance.getMetrics')
      const renderedFrames = (await frameCount()) - beforeFrames
      if (phase.startsWith('static')) expect(renderedFrames).toBe(0)
      else expect(renderedFrames).toBeGreaterThan(10)
      await page.evaluate(() => {
        const state = window as typeof window & { __inputDelays: number[]; __inputHandler: (event: KeyboardEvent) => void }
        state.__inputDelays = []
        state.__inputHandler = event => state.__inputDelays.push(performance.now() - event.timeStamp)
        document.addEventListener('keydown', state.__inputHandler)
      })
      for (let i = 0; i < 60; i++) await page.keyboard.press('Shift')
      const inputP95 = await page.evaluate(() => {
        const state = window as typeof window & { __inputDelays: number[]; __inputHandler: (event: KeyboardEvent) => void }
        document.removeEventListener('keydown', state.__inputHandler)
        const delays = state.__inputDelays.sort((a, b) => a - b)
        if (delays.length !== 60) throw new Error(`Expected 60 input events, got ${delays.length}`)
        return delays[Math.floor(delays.length * .95)]
      })
      const delta = (name: string) => (after.metrics.find(m=>m.name===name)?.value ?? 0)-(before.metrics.find(m=>m.name===name)?.value ?? 0)
      results.push({phase,...frames,renderedFrames,inputP95,taskSeconds:delta('TaskDuration'),scriptSeconds:delta('ScriptDuration')})
    }
    writeFileSync('/home/starboy/Projects/clover-desktop-setup/landing-performance-after-worker.json', JSON.stringify({gpu,results},null,2))
    const animated = results[1] as {longTasks:number}
    expect(animated.longTasks, 'ambient animation must not repeatedly block input with >50ms tasks').toBeLessThanOrEqual(1)
    const host = page.locator('[data-landing-botanical]').locator('..')
    await host.evaluate(el => { (el as HTMLElement).style.display = 'none' })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.waitForTimeout(500)
    const stopped = await frameCount()
    await page.waitForTimeout(500)
    expect(await frameCount()).toBe(stopped)
    await host.evaluate(el => { (el as HTMLElement).style.removeProperty('display') })
    await expect.poll(async () => (await frameCount()) - stopped).toBeGreaterThan(5)
  } finally { await fixture.cleanup() }
})

test('worker startup failure retains the static botanical fallback', async () => {
  const fixture = await setupMockBackend()
  try {
    const { page } = fixture
    await page.addInitScript(() => {
      Object.defineProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen', {
        value: () => { throw new Error('simulated unavailable canvas transfer') }, configurable: true,
      })
    })
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    const canvas = page.locator('[data-landing-botanical]')
    await expect(canvas).toBeVisible()
    await expect.poll(() => canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL().length)).toBeGreaterThan(10_000)
    await expect.poll(() => page.workers().filter(worker => worker.url().includes('landing-bloom.worker')).length).toBe(0)
    const image = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
    await page.waitForTimeout(500)
    expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(image)
  } finally { await fixture.cleanup() }
})
