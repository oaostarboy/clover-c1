import { expect, test } from '@playwright/test'

for (const mode of ['dark', 'light']) test(`normal scrolling keeps ${mode} glass`, async ({ page }, info) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await page.evaluate(mode => {
    const fixture = (window as any).delegationFixture
    fixture.theme(mode)
    document.documentElement.setAttribute('data-clover-glass', '')
    document.documentElement.style.setProperty('--translucency-glass-keep', '20%')
    document.body.style.backgroundImage = 'repeating-linear-gradient(110deg, #345766 0 30px, #8b775c 30px 60px)'
    fixture.scrollingMessages()
  }, mode)
  const viewport = page.locator('[data-slot="aui_thread-viewport"]')
  const row = page.locator('[data-slot="aui_user-message-root"]').first()
  const bubble = row.locator('.composer-human-message').first()
  await expect(bubble).toBeAttached()
  await viewport.evaluate(el => { el.scrollTop = 0 })
  await expect(row).toBeVisible()
  await expect(row).toHaveCSS('position', 'static')
  await expect(row).toHaveCSS('top', 'auto')
  await expect(row).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  const glass = await bubble.evaluate(el => ({ color: getComputedStyle(el).backgroundColor, filter: getComputedStyle(el).backdropFilter }))
  expect(glass.color).toMatch(/(0\.75|0\.7[0-9])/)
  expect(glass.filter).toContain('blur(')
  const dimensions = await row.evaluate(el => {
    const r = el.getBoundingClientRect(), b = el.querySelector('.composer-human-message')!.getBoundingClientRect()
    return { free: r.width - b.width, radius: getComputedStyle(el.querySelector('.composer-human-message')!).borderRadius }
  })
  expect(dimensions.free).toBeGreaterThan(80)
  expect(dimensions.radius).not.toBe('0px')
  await bubble.hover()
  expect(await bubble.evaluate(el => getComputedStyle(el).backdropFilter)).toContain('blur(')
  await expect(row).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await page.screenshot({ path: info.outputPath(`before-scroll-${mode}.png`) })
  const before = await row.evaluate(el => el.getBoundingClientRect().top)
  const delta = await viewport.evaluate(el => { const before = el.scrollTop; el.scrollTop += 180; return el.scrollTop - before })
  expect(delta).toBeGreaterThan(100)
  await expect.poll(() => row.evaluate(el => el.getBoundingClientRect().top)).toBeLessThan(before - 100)
  await page.screenshot({ path: info.outputPath(`after-scroll-${mode}.png`) })
  await viewport.evaluate(el => { el.scrollTop = 0 })
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  const copied = await bubble.evaluate(async el => {
    const text = el.querySelector('[data-slot="aui_user-inline-text"]')!
    const selection = window.getSelection()!
    const range = document.createRange()
    range.selectNodeContents(text)
    selection.removeAllRanges()
    selection.addRange(range)
    document.execCommand('copy')
    selection.removeAllRanges()
    return navigator.clipboard.readText()
  })
  expect(copied).toContain('Hello, I am testing normal scrolling.')
  await bubble.click()
  await expect(page.getByRole('textbox', { name: 'Edit message' })).toBeVisible()
})
