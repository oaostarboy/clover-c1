import fs from 'node:fs'
import path from 'node:path'

import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

const proof = process.env.CLOVER_VISUAL_PROOF ?? 'test-results/editorial-proof'

// Real Electron + sandbox gateway, deterministic fake inference. This does not
// claim live-model capability or use the user's profile, history or draft.
test('editorial track, expressive activity, lifecycle and answer transition', async () => {
  test.setTimeout(180_000)
  fs.mkdirSync(proof, { recursive: true })
  const fixture = await setupMockBackend({ mockServer: { holdFirstStreamForPrompt: 'E2E_VISUAL_FORMATION' } })
  const { page, app } = fixture
  try {
    await waitForAppReady(fixture, 120_000)
    await page.evaluate(() => localStorage.setItem('clover-desktop-mode-v1', 'dark'))
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.screenshot({ path: path.join(proof, 'opening.png') })
    const composer = page.locator('[contenteditable="true"]').first()
    await composer.fill('E2E_VISUAL_FORMATION. Find a clear direction for the desktop. Keep the useful parts, and make room for the answer.')
    await composer.press('Enter')
    const track = page.locator('[data-editorial-thread]')
    await expect(track).toBeVisible({ timeout: 30_000 })
    const formation = track.locator('canvas[data-clover-phase]').first()
    await expect(formation).toBeVisible({ timeout: 30_000 })
    await page.waitForTimeout(3500)
    await expect(formation).toBeVisible()
    const width = (await formation.boundingBox())!.width
    expect(width).toBeGreaterThanOrEqual(180)
    expect(width).toBeLessThanOrEqual(240)
    const bounds = await track.boundingBox()
    expect(bounds!.width).toBeLessThanOrEqual(834)
    const surface = await page.locator('[data-chat-surface]').first().boundingBox()
    expect(Math.abs(bounds!.x + bounds!.width / 2 - (surface!.x + surface!.width / 2))).toBeLessThan(12)
    await page.screenshot({ path: path.join(proof, 'working-wide.png') })

    // Measure just this formation's JS paint work, not landing's separate worker.
    await page.evaluate(() => {
      const data = { frames: 0, ms: [] as number[], fills: 0, start: 0 }
      ;(window as any).__cloverPaint = data
      const clear = CanvasRenderingContext2D.prototype.clearRect
      const fill = CanvasRenderingContext2D.prototype.fill
      CanvasRenderingContext2D.prototype.clearRect = function (...args) {
        if ((this.canvas as HTMLCanvasElement).dataset.cloverPhase) { data.frames++; data.fills = 0; data.start = performance.now() }
        return clear.apply(this, args)
      }
      CanvasRenderingContext2D.prototype.fill = function (this: CanvasRenderingContext2D, ...args: any[]) {
        const result = (fill as any).apply(this, args)
        if ((this.canvas as HTMLCanvasElement).dataset.cloverPhase && ++data.fills === 6) data.ms.push(performance.now() - data.start)
        return result
      } as typeof fill
    })
    await page.waitForTimeout(3000)
    const running = await page.evaluate(() => (window as any).__cloverPaint)
    expect(running.frames).toBeGreaterThan(1) // shared-host scheduling may run below the 24fps cap
    expect(running.frames).toBeLessThanOrEqual(80)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const still = await formation.evaluate((el: HTMLCanvasElement) => el.toDataURL())
    const reducedFrames = await page.evaluate(() => (window as any).__cloverPaint.frames)
    await page.waitForTimeout(350)
    expect(await formation.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(still)
    expect(await page.evaluate(() => (window as any).__cloverPaint.frames)).toBe(reducedFrames)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    // Native hide uses the existing typed window-state bridge even while the
    // shell keeps Chromium visibility active for streaming.
    const chatWindow = await app.browserWindow(page)
    await chatWindow.evaluate(win => win.hide())
    await page.waitForTimeout(150)
    const hiddenFrames = await page.evaluate(() => (window as any).__cloverPaint.frames)
    await page.waitForTimeout(350)
    expect(await page.evaluate(() => (window as any).__cloverPaint.frames)).toBe(hiddenFrames)
    await chatWindow.evaluate(win => win.showInactive())
    await expect.poll(() => page.evaluate(() => (window as any).__cloverPaint.frames)).toBeGreaterThan(hiddenFrames)
    const metrics = { ...running, reducedFrames, hiddenFrames, width, trackWidth: bounds!.width }
    fs.writeFileSync(path.join(proof, 'formation-metrics.json'), JSON.stringify(metrics, null, 2))

    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const width of [600, 375]) {
      await page.setViewportSize({ width, height: 820 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      await expect(formation).toBeVisible()
      await page.screenshot({ path: path.join(proof, `working-${width}.png`) })
    }
    await page.setViewportSize({ width: 1280, height: 900 })
    fixture.mock.releaseHeldStream()
    await expect(track.locator('canvas[data-clover-phase]')).toHaveCount(0, { timeout: 60_000 })
    await expect(track.getByText(/full boot chain is working/).first()).toBeVisible()
    await page.screenshot({ path: path.join(proof, 'answer-wide.png') })
    const prompt = track.locator('.composer-human-message').first()
    const answer = track.locator('[data-slot="aui_assistant-message-content"]').first()
    expect(Math.abs((await prompt.boundingBox())!.x - (await answer.boundingBox())!.x)).toBeLessThan(2)
    await page.keyboard.press('Control+k')
    await expect(page.getByRole('dialog').first()).toBeVisible()
    await page.keyboard.press('Escape')
    await page.keyboard.press('Control+Shift+f')
    await expect(page.getByRole('textbox', { name: /search sessions/i })).toBeFocused()
  } finally {
    fixture.mock.releaseHeldStream()
    await fixture.cleanup()
  }
})
