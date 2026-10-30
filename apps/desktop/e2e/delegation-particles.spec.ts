import { expect, test, type Page } from '@playwright/test'

// Optional real Chromium recordings for this production-component fixture.
test.use({ video: process.env.RECORD_PARTICLE_EVIDENCE === '1' ? 'on' : 'off' })
const fixture = async (page: Page, method: string, ...args: unknown[]) =>
  page.evaluate(({ method, args }) => (window as any).delegationFixture[method](...args), { method, args })
test('live helper canvas animates internally after splitting settles', async ({ page }) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await expect(page.locator('.clover-activity-art')).toBeVisible()
  await fixture(page, 'spawn', 'motion')
  const canvas = page.locator('.delegation-flight canvas')
  await expect(page.locator('.delegation-flight')).toHaveCSS('visibility', 'visible')
  await page.waitForTimeout(900)
  const position = await canvas.boundingBox()
  const first = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.screenshot({ path: test.info().outputPath('helper-motion.png') })
  await page.waitForTimeout(300)
  const second = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  expect(await canvas.boundingBox()).toEqual(position)
  expect(second).not.toBe(first)
})
test('main particle continues across waiting, tool and working and pauses when hidden', async ({ page }) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  const canvas = page.locator('.clover-activity-art canvas')
  await expect(canvas).toBeVisible()
  await page.waitForTimeout(900)
  for (const phase of ['waiting', 'tool', 'working']) {
    await fixture(page, 'phase', phase)
    await expect(canvas).toHaveAttribute('data-clover-phase', phase)
    await page.screenshot({ path: test.info().outputPath(`main-${phase}.png`) })
    const first = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
    await page.waitForTimeout(300)
    expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(first)
    expect(await page.locator('.clover-activity-art canvas').count()).toBe(1)
  }
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  const paused = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(paused)
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(paused)
})
test('reduced motion and completed helper stay still', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await fixture(page, 'spawn', 'still')
  await fixture(page, 'spawn', 'active')
  const canvas = page.locator('[data-helper-berth="still"] canvas')
  await expect(canvas).toBeVisible()
  const first = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(first)
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await fixture(page, 'terminal', 'still', 'completed')
  await expect(page.locator('[data-helper-berth="still"]')).toHaveAttribute('data-status', 'completed')
  await page.waitForTimeout(100)
  const completed = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(completed)
})
test('live helper animates while docking and at berth; narrow labels do not wrap or overflow', async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 900 })
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await fixture(page, 'spawn', 'first')
  await fixture(page, 'spawn', 'second')
  await page.waitForTimeout(850)
  const label = page.locator('.delegation-strip-toggle').getByText('2 Subagents', { exact: true })
  await expect(label).toBeVisible()
  for (const width of [320, 360, 420, 760]) {
    await page.setViewportSize({ width, height: 900 })
    const geometry = await label.evaluate(el => {
      const range = document.createRange()
      range.selectNodeContents(el)
      const box = el.getBoundingClientRect()
      const parent = el.closest('button')!.getBoundingClientRect()
      return {
        lines: new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size,
        left: box.left,
        right: box.right,
        parentLeft: parent.left,
        parentRight: parent.right,
        width: el.clientWidth,
        contentWidth: el.scrollWidth,
        rowWidth: el.closest('button')!.clientWidth,
        rowContentWidth: el.closest('button')!.scrollWidth
      }
    })
    expect(geometry.lines).toBe(1)
    expect(geometry.left).toBeGreaterThanOrEqual(geometry.parentLeft)
    expect(geometry.right).toBeLessThanOrEqual(geometry.parentRight)
    expect(geometry.contentWidth).toBeLessThanOrEqual(geometry.width)
    expect(geometry.rowContentWidth).toBeLessThanOrEqual(geometry.rowWidth)
  }
  await page.setViewportSize({ width: 760, height: 900 })
  const flight = page.locator('.delegation-flight').first()
  const canvas = flight.locator('canvas')
  const before = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await fixture(page, 'finish')
  // The main formation must depart before the helpers start their dock.
  await expect(page.locator('.clover-activity-art canvas')).toHaveCount(0)
  await expect(flight).toHaveCSS('visibility', 'visible')
  const docking = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(120)
  await expect(flight).toHaveCSS('visibility', 'visible')
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(docking)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(before)
  expect((await fixture(page, 'state')).every((agent: any) => agent.status === 'running')).toBe(true)
  await expect(flight).toHaveCSS('visibility', 'hidden')
  const berth = page.locator('[data-helper-berth="first"] canvas')
  await expect(berth).toBeVisible()
  const docked = await berth.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(300)
  expect(await berth.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(docked)
  await page.screenshot({ path: test.info().outputPath('docked-motion.png') })
})
test('offscreen main canvas pauses and resumes', async ({ page }) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  const canvas = page.locator('.clover-activity-art canvas')
  await expect(canvas).toBeVisible()
  await page.waitForTimeout(800)
  await canvas.evaluate(el => {
    ;(el as HTMLElement).style.transform = 'translateY(300vh)'
  })
  await page.waitForTimeout(200)
  const paused = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(paused)
  await canvas.evaluate(el => {
    ;(el as HTMLElement).style.transform = ''
  })
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(paused)
})
test('native renderer pause boundary stops canvas frames and resumes them', async ({ page }) => {
  await page.addInitScript(() => {
    const listeners: ((payload: object) => void)[] = []
    ;(window as any).cloverDesktop = {
      onWindowStateChanged: (listener: (payload: object) => void) => {
        listeners.push(listener)
        return () => listeners.splice(listeners.indexOf(listener), 1)
      }
    }
    ;(window as any).fixtureWindowState = (payload: object) => listeners.slice().forEach(listener => listener(payload))
  })
  await page.goto('/e2e/fixtures/delegation-particles.html')
  const canvas = page.locator('.clover-activity-art canvas')
  await expect(canvas).toBeVisible()
  await page.waitForTimeout(800)
  await page.evaluate(() => (window as any).fixtureWindowState({ isMinimized: true, isVisible: false }))
  const paused = await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(paused)
  await page.evaluate(() => (window as any).fixtureWindowState({ isMinimized: false, isVisible: true }))
  await page.waitForTimeout(300)
  expect(await canvas.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(paused)
})
for (const width of [760, 1000, 1440]) {
  test(`real components: spawn, dock, disclose and switch at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('/e2e/fixtures/delegation-particles.html')
    await fixture(page, 'theme', 'dark')
    await expect(page.locator('html')).toHaveClass(/dark/)
    await expect(page.locator('.clover-activity-art')).toBeVisible()
    const beforeControls = await page.locator('[data-slot="composer-rich-input"]').count()
    expect(beforeControls).toBeGreaterThan(0)
    expect(await page.locator('.wordmark').evaluate(el => getComputedStyle(el).fontFamily)).not.toContain('Collapse')
    await expect(page.locator('body')).not.toContainText('ASYNC DELEGATION BATCH COMPLETE')
    await expect(page.locator('body')).not.toContainText('[SILENT]')
    await fixture(page, 'spawn', 'a')
    await expect(page.locator('[data-helper-berth]')).toHaveCount(1)
    // Sample the entire split, not only its final frame. The visible mini must
    // stay in the dedicated lane, away from the disclosure label below it.
    let sawFlight = false
    for (let frame = 0; frame < 18; frame++) {
      sawFlight ||= await page
        .locator('.delegation-flight')
        .evaluateAll(els => els.some(el => getComputedStyle(el).visibility === 'visible'))
      const overlaps = await page.evaluate(() => {
        const summaries = [
          ...document.querySelectorAll(
            '[data-slot="aui_assistant-message-content"] p, [data-slot="aui_assistant-message-content"] pre, [data-slot="aui_user-message-root"], [data-conversation-scaffold], summary'
          )
        ].map(el => el.getBoundingClientRect())
        return [...document.querySelectorAll('.delegation-flight')]
          .filter(el => getComputedStyle(el).visibility === 'visible')
          .some(el => {
            const r = el.getBoundingClientRect()
            return summaries.some(
              s => s.width && s.height && r.left < s.right && r.right > s.left && r.top < s.bottom && r.bottom > s.top
            )
          })
      })
      expect(overlaps).toBe(false)
      await page.waitForTimeout(45)
    }
    expect(sawFlight).toBe(true)
    await page.screenshot({ path: info.outputPath(`budding-${width}.png`) })
    for (const id of ['b', 'c', 'd', 'e', 'f']) await fixture(page, 'spawn', id)
    await fixture(page, 'todos')
    await expect(page.getByText('Inspect the desktop', { exact: true })).toBeVisible()
    await expect(page.getByText('Verify the helpers', { exact: true })).toBeVisible()
    await expect(page.locator('[data-slot="composer-status-stack"]')).toContainText('1/2')
    await expect(page.locator('[data-helper-berth]')).toHaveCount(4)
    await expect(page.locator('.delegation-berths')).toContainText('+2')
    await fixture(
      page,
      'thinking',
      'a',
      'Actual fixture provider summary.\n\n' + 'Complete readable detail. '.repeat(25)
    )
    await fixture(page, 'finish')
    // Follow the complete handoff, including the main formation's departure.
    let sawDockFlight = false
    for (let frame = 0; frame < 34; frame++) {
      const sample = await page.evaluate(() => {
        const text = [
          ...document.querySelectorAll(
            '[data-slot="aui_assistant-message-content"] p, [data-slot="aui_assistant-message-content"] pre, [data-slot="aui_user-message-root"], [data-conversation-scaffold], summary'
          )
        ].map(el => el.getBoundingClientRect())
        const flights = [...document.querySelectorAll('.delegation-flight')].filter(
          el => getComputedStyle(el).visibility === 'visible'
        )
        return {
          visible: flights.length,
          overlaps: flights.some(el => {
            const r = el.getBoundingClientRect()
            return text.some(
              s =>
                s.width > 1 &&
                s.height > 1 &&
                r.left < s.right &&
                r.right > s.left &&
                r.top < s.bottom &&
                r.bottom > s.top
            )
          })
        }
      })
      sawDockFlight ||= sample.visible > 0
      expect(sample.overlaps).toBe(false)
      await page.waitForTimeout(35)
    }
    expect(sawDockFlight).toBe(true)
    expect((await fixture(page, 'state')).every((agent: any) => agent.status === 'running')).toBe(true)
    expect(
      await page
        .locator('.delegation-flight')
        .evaluateAll(els => els.every(el => getComputedStyle(el).visibility === 'hidden'))
    ).toBe(true)
    await page.locator('.delegation-strip-toggle').click()
    await expect(page.getByText('Verify the helpers', { exact: true })).toBeVisible()
    const expandedBounds = await page.evaluate(() => {
      const row = document.querySelector('[data-delegate-card]')!.getBoundingClientRect()
      const stack = document.querySelector('[data-slot="composer-status-stack"]')!.getBoundingClientRect()
      const composer = document.querySelector('[data-slot="composer-rich-input"]')!.getBoundingClientRect()
      return { rowBottom: row.bottom, stackTop: stack.top, stackBottom: stack.bottom, composerTop: composer.top }
    })
    expect(expandedBounds.rowBottom).toBeLessThanOrEqual(expandedBounds.stackTop + 1)
    expect(expandedBounds.stackBottom).toBeLessThanOrEqual(expandedBounds.composerTop + 1)
    await page.screenshot({ path: info.outputPath(`dark-expanded-checklist-${width}.png`) })
    await page.getByText('Recent thinking summaries', { exact: true }).click()
    await expect(
      page.locator('.delegation-stream p').filter({ hasText: 'Actual fixture provider summary' })
    ).toContainText('Complete readable detail. '.repeat(25).trim())
    const stops = page.getByRole('button', { name: 'Stop', exact: true })
    expect(await stops.count()).toBe(6)
    for (let index = 0; index < (await stops.count()); index++) {
      await stops.nth(index).scrollIntoViewIfNeeded()
      expect(
        await stops.nth(index).evaluate(el => {
          const r = el.getBoundingClientRect()
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
          return !!hit && (hit === el || el.contains(hit))
        })
      ).toBe(true)
    }
    await stops.first().click()
    expect(
      (await fixture(page, 'requests')).some(
        (r: any) => r.method === 'subagent.interrupt' && r.params.subagent_id === 'a'
      )
    ).toBe(true)
    await fixture(page, 'terminal', 'a', 'failed')
    await page.locator('.delegation-strip-toggle').click()
    await expect(page.locator('.delegation-attention')).toContainText('Backend failed')
    await expect(page.locator('.delegation-strip-toggle')).toContainText('5 Running')
    await expect(page.locator('.delegation-strip-toggle')).toContainText('1 Failed')
    const clearance = await page.evaluate(() => {
      const threadRow = document.querySelector('[data-delegate-card]')!.getBoundingClientRect()
      const stack = document.querySelector('[data-slot="composer-status-stack"]')!.getBoundingClientRect()
      const composer = document.querySelector('[data-slot="composer-rich-input"]')!.getBoundingClientRect()
      return {
        threadBottom: threadRow.bottom,
        stackTop: stack.top,
        stackBottom: stack.bottom,
        composerTop: composer.top,
        viewport: innerHeight
      }
    })
    expect(clearance.threadBottom).toBeLessThanOrEqual(clearance.stackTop + 1)
    expect(clearance.stackBottom).toBeLessThanOrEqual(clearance.composerTop + 1)
    expect(clearance.composerTop).toBeLessThan(clearance.viewport)
    await page.screenshot({ path: info.outputPath(`docked-${width}.png`) })
    await fixture(page, 'theme', 'light')
    await expect(page.locator('html')).not.toHaveClass(/dark/)
    await page.screenshot({ path: info.outputPath(`light-checklist-${width}.png`) })
    expect(await page.locator('[data-slot="composer-rich-input"]').count()).toBe(beforeControls)
    const opaque = await page
      .locator('[data-slot="aui_user-message-root"]')
      .first()
      .evaluate(el => getComputedStyle(el).backgroundColor)
    expect(opaque).toBe('rgba(0, 0, 0, 0)')
    expect(await page.locator('.sticky-human-clamp').first().getAttribute('data-clamped')).toBeNull()
    await fixture(page, 'switchSession')
    await expect(page.locator('[data-helper-berth]')).toHaveCount(0)
    await expect(page.locator('.delegation-flight')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
test('reduced motion settles immediately without changing backend state', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await expect(page.locator('.clover-activity-art')).toBeVisible()
  await fixture(page, 'spawn', 'reduced')
  await expect(page.locator('[data-helper-berth]')).toHaveCount(1)
  expect(await page.locator('.delegation-flight').evaluate(el => getComputedStyle(el).visibility)).toBe('hidden')
  expect((await fixture(page, 'state'))[0].status).toBe('running')
})
test('offscreen berth pauses and resumes its flight without lifecycle changes', async ({ page }) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await expect(page.locator('.clover-activity-art')).toBeVisible()
  await fixture(page, 'spawn', 'offscreen')
  const berth = page.locator('[data-helper-berth]')
  await berth.evaluate(el => {
    ;(el as HTMLElement).style.transform = 'translateY(200vh)'
  })
  await expect(page.locator('.delegation-flight')).toHaveCSS('visibility', 'hidden')
  const transform = await page.locator('.delegation-flight').evaluate(el => (el as HTMLElement).style.transform)
  await page.waitForTimeout(180)
  expect(await page.locator('.delegation-flight').evaluate(el => (el as HTMLElement).style.transform)).toBe(transform)
  expect((await fixture(page, 'state'))[0].status).toBe('running')
  await berth.evaluate(el => {
    ;(el as HTMLElement).style.transform = ''
  })
  await expect(page.locator('.delegation-flight')).toHaveCSS('visibility', 'visible')
  await fixture(page, 'switchSession')
  await expect(page.locator('.delegation-flight')).toHaveCount(0)
})
test('hidden document pauses flight and session switch cleans it up', async ({ page }) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await expect(page.locator('.clover-activity-art')).toBeVisible()
  await fixture(page, 'spawn', 'hidden')
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.locator('.delegation-flight')).toHaveCSS('visibility', 'hidden')
  expect((await fixture(page, 'state'))[0].status).toBe('running')
  await fixture(page, 'switchSession')
  await expect(page.locator('.delegation-flight')).toHaveCount(0)
})
