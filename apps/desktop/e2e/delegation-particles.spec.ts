import { expect, test, type Page } from '@playwright/test'
const fixture = async (page: Page, method: string, ...args: unknown[]) => page.evaluate(({ method, args }) => (window as any).delegationFixture[method](...args), { method, args })
for (const width of [760, 1000, 1440]) {
  test(`real components: spawn, dock, disclose and switch at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('/e2e/fixtures/delegation-particles.html')
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
        const summaries = [...document.querySelectorAll('summary')].map(el => el.getBoundingClientRect())
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
    await expect(page.locator('[data-helper-berth]')).toHaveCount(4)
    await expect(page.locator('.delegation-berths')).toContainText('+2')
    await fixture(page, 'thinking', 'a', 'Actual fixture provider summary.\n\n' + 'Complete readable detail. '.repeat(25))
    await fixture(page, 'finish')
    await page.waitForTimeout(1000)
    expect((await fixture(page, 'state')).every((agent: any) => agent.status === 'running')).toBe(true)
    expect(await page.locator('.delegation-flight').evaluateAll(els => els.every(el => getComputedStyle(el).visibility === 'hidden'))).toBe(true)
    await page.locator('.delegation-strip-toggle').click()
    await page.getByText('Recent thinking summaries', { exact: true }).click()
    await expect(page.locator('.delegation-stream p').filter({ hasText: 'Actual fixture provider summary' })).toContainText('Complete readable detail. '.repeat(25).trim())
    await page.getByRole('button', { name: 'Stop', exact: true }).first().click()
    expect((await fixture(page, 'requests')).some((r: any) => r.method === 'subagent.interrupt' && r.params.subagent_id === 'a')).toBe(true)
    await fixture(page, 'terminal', 'a', 'failed')
    await page.locator('.delegation-strip-toggle').click()
    await expect(page.locator('.delegation-attention')).toContainText('Backend failed')
    await page.screenshot({ path: info.outputPath(`docked-${width}.png`) })
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
