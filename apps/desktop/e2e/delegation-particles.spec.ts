import { expect, test, type Page } from '@playwright/test'
const fixture = async (page: Page, method: string, ...args: unknown[]) => page.evaluate(({ method, args }) => (window as any).delegationFixture[method](...args), { method, args })
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
      sawFlight ||= await page.locator('.delegation-flight').evaluateAll(els => els.some(el => getComputedStyle(el).visibility === 'visible'))
      const overlaps = await page.evaluate(() => {
        const summaries = [...document.querySelectorAll('[data-slot="aui_assistant-message-content"] p, [data-slot="aui_assistant-message-content"] pre, [data-slot="aui_user-message-root"], [data-conversation-scaffold], summary')].map(el => el.getBoundingClientRect())
        return [...document.querySelectorAll('.delegation-flight')].filter(el => getComputedStyle(el).visibility === 'visible').some(el => {
          const r = el.getBoundingClientRect()
          return summaries.some(s => s.width && s.height && r.left < s.right && r.right > s.left && r.top < s.bottom && r.bottom > s.top)
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
    await fixture(page, 'thinking', 'a', 'Actual fixture provider summary.\n\n' + 'Complete readable detail. '.repeat(25))
    await fixture(page, 'finish')
    // Follow the complete handoff, including the main formation's departure.
    let sawDockFlight = false
    for (let frame = 0; frame < 34; frame++) {
      const sample = await page.evaluate(() => {
        const text = [...document.querySelectorAll('[data-slot="aui_assistant-message-content"] p, [data-slot="aui_assistant-message-content"] pre, [data-slot="aui_user-message-root"], [data-conversation-scaffold], summary')].map(el => el.getBoundingClientRect())
        const flights = [...document.querySelectorAll('.delegation-flight')].filter(el => getComputedStyle(el).visibility === 'visible')
        return { visible: flights.length, overlaps: flights.some(el => {
          const r = el.getBoundingClientRect()
          return text.some(s => s.width > 1 && s.height > 1 && r.left < s.right && r.right > s.left && r.top < s.bottom && r.bottom > s.top)
        }) }
      })
      sawDockFlight ||= sample.visible > 0
      expect(sample.overlaps).toBe(false)
      await page.waitForTimeout(35)
    }
    expect(sawDockFlight).toBe(true)
    expect((await fixture(page, 'state')).every((agent: any) => agent.status === 'running')).toBe(true)
    expect(await page.locator('.delegation-flight').evaluateAll(els => els.every(el => getComputedStyle(el).visibility === 'hidden'))).toBe(true)
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
    await expect(page.locator('.delegation-stream p').filter({ hasText: 'Actual fixture provider summary' })).toContainText('Complete readable detail. '.repeat(25).trim())
    const stops = page.getByRole('button', { name: 'Stop', exact: true })
    expect(await stops.count()).toBe(6)
    for (let index = 0; index < await stops.count(); index++) {
      await stops.nth(index).scrollIntoViewIfNeeded()
      expect(await stops.nth(index).evaluate(el => {
        const r = el.getBoundingClientRect()
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return !!hit && (hit === el || el.contains(hit))
      })).toBe(true)
    }
    await stops.first().click()
    expect((await fixture(page, 'requests')).some((r: any) => r.method === 'subagent.interrupt' && r.params.subagent_id === 'a')).toBe(true)
    await fixture(page, 'terminal', 'a', 'failed')
    await page.locator('.delegation-strip-toggle').click()
    await expect(page.locator('.delegation-attention')).toContainText('Backend failed')
    await expect(page.locator('.delegation-strip-toggle')).toContainText('5 Running')
    await expect(page.locator('.delegation-strip-toggle')).toContainText('1 Failed')
    const clearance = await page.evaluate(() => {
      const threadRow = document.querySelector('[data-delegate-card]')!.getBoundingClientRect()
      const stack = document.querySelector('[data-slot="composer-status-stack"]')!.getBoundingClientRect()
      const composer = document.querySelector('[data-slot="composer-rich-input"]')!.getBoundingClientRect()
      return { threadBottom: threadRow.bottom, stackTop: stack.top, stackBottom: stack.bottom, composerTop: composer.top, viewport: innerHeight }
    })
    expect(clearance.threadBottom).toBeLessThanOrEqual(clearance.stackTop + 1)
    expect(clearance.stackBottom).toBeLessThanOrEqual(clearance.composerTop + 1)
    expect(clearance.composerTop).toBeLessThan(clearance.viewport)
    await page.screenshot({ path: info.outputPath(`docked-${width}.png`) })
    await fixture(page, 'theme', 'light')
    await expect(page.locator('html')).not.toHaveClass(/dark/)
    await page.screenshot({ path: info.outputPath(`light-checklist-${width}.png`) })
    expect(await page.locator('[data-slot="composer-rich-input"]').count()).toBe(beforeControls)
    const opaque = await page.locator('[data-slot="aui_user-message-root"]').first().evaluate(el => getComputedStyle(el).backgroundColor)
    expect(opaque).not.toBe('rgba(0, 0, 0, 0)')
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
  await berth.evaluate(el => { (el as HTMLElement).style.transform = 'translateY(200vh)' })
  await expect(page.locator('.delegation-flight')).toHaveCSS('visibility', 'hidden')
  const transform = await page.locator('.delegation-flight').evaluate(el => (el as HTMLElement).style.transform)
  await page.waitForTimeout(180)
  expect(await page.locator('.delegation-flight').evaluate(el => (el as HTMLElement).style.transform)).toBe(transform)
  expect((await fixture(page, 'state'))[0].status).toBe('running')
  await berth.evaluate(el => { (el as HTMLElement).style.transform = '' })
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
