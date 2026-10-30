import { expect, test } from '@playwright/test'
for (const mode of ['dark','light']) test(`glass messages mask only the bubble in ${mode}`, async ({ page }, info) => {
  await page.goto('/e2e/fixtures/delegation-particles.html')
  await page.evaluate(mode => (window as any).delegationFixture.theme(mode), mode)
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-clover-glass','')
    document.documentElement.style.setProperty('--translucency-glass-keep','20%')
    document.body.style.backgroundImage='repeating-linear-gradient(110deg, #345766 0 30px, #8b775c 30px 60px)'
    ;(window as any).delegationFixture.messages('Hello, I am testing.')
  })
  const row=page.locator('[data-slot="aui_user-message-root"]').first()
  const bubble=row.locator('.composer-human-message').first()
  await expect(bubble).toBeVisible()
  await expect(row).toHaveCSS('background-color','rgba(0, 0, 0, 0)')
  expect(await bubble.evaluate(el=>getComputedStyle(el).backgroundImage)).toContain('linear-gradient')
  expect(await bubble.evaluate(el=>getComputedStyle(el).backgroundColor)).not.toBe('rgba(0, 0, 0, 0)')
  const dimensions=await row.evaluate(el=>{const r=el.getBoundingClientRect(),b=el.querySelector('.composer-human-message')!.getBoundingClientRect();return {free:r.width-b.width,radius:getComputedStyle(el.querySelector('.composer-human-message')!).borderRadius}})
  expect(dimensions.free).toBeGreaterThan(80)
  expect(dimensions.radius).not.toBe('0px')
  await bubble.hover()
  expect(await bubble.evaluate(el=>getComputedStyle(el).backgroundImage)).toContain('linear-gradient')
  await expect(row).toHaveCSS('background-color','rgba(0, 0, 0, 0)')
  await page.screenshot({path:info.outputPath(`glass-${mode}.png`)})
})
