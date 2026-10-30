import { expect, test } from '@playwright/test'

for (const mode of ['dark', 'light']) {
  test(`sticky glass excludes transcript ink in ${mode}`, async ({ page }, info) => {
    await page.goto('/e2e/fixtures/delegation-particles.html')
    await page.waitForFunction(() => Boolean((window as any).delegationFixture))
    await page.evaluate(mode => {
      ;(window as any).delegationFixture.theme(mode)
      document.documentElement.setAttribute('data-clover-glass', '')
      document.documentElement.style.setProperty('--translucency-glass-keep', '20%')
      document.body.style.backgroundImage = 'repeating-linear-gradient(110deg, #345766 0 30px, #8b775c 30px 60px)'
      ;(window as any).delegationFixture.glassMessages()
    }, mode)
    const row = page.locator('[data-message-id="glass-three"]')
    const bubble = row.locator('.composer-human-message').first()
    const viewport = page.locator('[data-slot="aui_thread-viewport"]')
    const answer = page.locator('[data-slot="aui_assistant-message-root"]').last()
    await expect(bubble).toBeVisible()
    await viewport.evaluate(el => { el.scrollTop = el.scrollHeight / 2 })
    await expect.poll(async () => viewport.evaluate(el => el.scrollTop)).toBeGreaterThan(100)
    await answer.evaluate(el => { el.querySelectorAll('p').forEach(p => { p.style.color = '#ff0033' }) })
    await page.waitForTimeout(300)
    await page.screenshot({ path: info.outputPath(`glass-${mode}-actual.png`) })
    const geometry = await bubble.evaluate(el => {
      const bubble = el.getBoundingClientRect()
      const answer = document.querySelectorAll('[data-slot="aui_assistant-message-root"]')
      const rect = answer[answer.length - 1].getBoundingClientRect()
      return { bubbleTop: bubble.top, bubbleBottom: bubble.bottom, answerTop: rect.top, answerBottom: rect.bottom }
    })
    expect(geometry.answerTop).toBeLessThan(geometry.bubbleBottom)
    expect(geometry.answerBottom).toBeGreaterThan(geometry.bubbleTop)
    const glyphOverlap = await bubble.evaluate(el => {
      const b = el.getBoundingClientRect()
      return [...document.querySelectorAll('[data-slot="aui_assistant-message-content"] p')].some(p => {
        const range = document.createRange()
        range.selectNodeContents(p)
        return [...range.getClientRects()].some(r => r.top < b.bottom && r.bottom > b.top && r.left < b.right && r.right > b.left)
      })
    })
    expect(glyphOverlap).toBe(true)
    await expect(row).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    const alpha = await bubble.evaluate(el => {
      const color = getComputedStyle(el).backgroundColor
      const canvas = document.createElement('canvas')
      canvas.width = canvas.height = 1
      const ctx = canvas.getContext('2d')!
      ctx.fillStyle = color
      ctx.fillRect(0, 0, 1, 1)
      return ctx.getImageData(0, 0, 1, 1).data[3]
    })
    expect(alpha).toBeGreaterThan(0)
    expect(alpha).toBeLessThan(255)
    const clip = (await bubble.boundingBox())!
    expect(await answer.evaluate(el => getComputedStyle(el).clipPath)).toContain('evenodd')
    const wholeBefore = await page.screenshot({ animations: 'disabled' })
    const actual = await page.screenshot({ clip, animations: 'disabled' })
    await answer.evaluate(el => { (el as HTMLElement).style.visibility = 'hidden' })
    const reference = await page.screenshot({ clip, animations: 'disabled' })
    const wholeAfter = await page.screenshot({ animations: 'disabled' })
    expect(wholeBefore.equals(wholeAfter), 'hiding the answer must change the viewport').toBe(false)
    await page.screenshot({ path: info.outputPath(`glass-${mode}-reference.png`) })
    expect(actual.equals(reference), 'assistant ink must not change pixels inside the bubble').toBe(true)
    await answer.evaluate(el => { (el as HTMLElement).style.visibility = '' })
    await page.setViewportSize({ width: 1100, height: 650 })
    await bubble.click()
    await expect(page.locator('[data-slot="aui_edit-composer-root"]')).toBeVisible()
  })
}
