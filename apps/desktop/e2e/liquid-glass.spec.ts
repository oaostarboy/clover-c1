import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

test('glass controls retain interaction and accessible opaque fallbacks', async () => {
  const fixture = await setupMockBackend()
  try {
    await waitForAppReady(fixture, 120_000)
    const page = fixture.page
    await page.evaluate(() => localStorage.setItem('clover-desktop-mode-v1', 'dark'))
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const surface = page.locator('[data-slot="composer-surface"]')
    await expect(surface).toBeVisible()
    // A moving high-contrast shape behind the actual composer tests transmitted
    // pixels, not just a computed CSS string. Compare its edge with blur off.
    await page.evaluate(() => {
      const s = document.querySelector('[data-slot="composer-surface"]')!.getBoundingClientRect()
      const bounds = document.querySelector('[data-slot="composer-bounds"]')!
      const b = bounds.getBoundingClientRect()
      const probe = document.createElement('div')
      probe.id = 'glass-pixel-probe'
      Object.assign(probe.style, { position: 'absolute', zIndex: '25', left: `${s.left - b.left + 150}px`, top: `${s.top - b.top + 10}px`, width: '180px', height: '65px', background: 'rgb(20,240,100)', pointerEvents: 'none' })
      bounds.appendChild(probe)
    })
    const pixel = async (x: number, y: number) => {
      const image = (await surface.screenshot({ animations: 'disabled' })).toString('base64')
      return page.evaluate(async ({ image, x, y }) => {
        const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${image}`)).blob())
        const canvas = document.createElement('canvas')
        canvas.width = bitmap.width; canvas.height = bitmap.height
        const ctx = canvas.getContext('2d')!
        ctx.drawImage(bitmap, 0, 0)
        return ctx.getImageData(x, y, 1, 1).data[1]
      }, { image, x, y })
    }
    const filteredEdge = await pixel(160, 40)
    const filteredBefore = await pixel(130, 40)
    expect(filteredEdge).toBeGreaterThan(filteredBefore + 20)
    await surface.evaluate(el => { (el as HTMLElement).style.backdropFilter = 'none' })
    const sharpEdge = await pixel(160, 40)
    expect(sharpEdge).toBeGreaterThan(filteredEdge + 30)
    await surface.evaluate(el => { (el as HTMLElement).style.backdropFilter = '' })
    await page.evaluate(() => { const p = document.querySelector('#glass-pixel-probe') as HTMLElement; p.style.left = `${parseFloat(p.style.left) + 200}px` })
    expect(await pixel(160, 40)).toBeLessThan(filteredEdge - 20)
    await page.evaluate(() => document.querySelector('#glass-pixel-probe')?.remove())
    const material = () => surface.evaluate(el => {
      const css = getComputedStyle(el)
      return { blur: css.backdropFilter, fill: css.backgroundColor, radius: css.borderRadius }
    })
    expect((await material()).blur).not.toContain('url(')
    expect((await material()).blur).toBe('blur(20px) saturate(1.15)')
    const underlay = surface.locator(':scope > [aria-hidden="true"]').first()
    expect(await underlay.evaluate(el => getComputedStyle(el).backgroundColor)).toBe('rgba(0, 0, 0, 0)')
    expect((await material()).radius).toBe('14px')
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Emulation.setEmulatedMedia', { features: [
      { name: 'prefers-reduced-transparency', value: 'reduce' },
      { name: 'prefers-reduced-motion', value: 'reduce' }
    ] })
    expect((await material()).blur).toBe('none')
    expect((await material()).fill).not.toBe('rgba(0, 0, 0, 0)')
    await cdp.send('Emulation.setEmulatedMedia', { features: [
      { name: 'prefers-contrast', value: 'more' },
      { name: 'prefers-reduced-motion', value: 'reduce' }
    ] })
    expect((await material()).blur).toBe('none')
    await cdp.send('Emulation.setEmulatedMedia', { features: [
      { name: 'prefers-reduced-transparency', value: 'no-preference' },
      { name: 'prefers-reduced-motion', value: 'reduce' }
    ] })
    await page.screenshot({ path: process.env.CLOVER_GLASS_PROOF ?? '/home/starboy/Projects/clover-desktop-setup/frost-desktop.png', animations: 'disabled' })
    await surface.screenshot({ path: '/home/starboy/Projects/clover-desktop-setup/frost-close.png', animations: 'disabled' })
    await page.setViewportSize({ width: 600, height: 800 })
    await expect(surface).toBeVisible()
    expect(await surface.evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(600)
    await page.screenshot({ path: '/home/starboy/Projects/clover-desktop-setup/frost-narrow.png', animations: 'disabled' })
    await page.setViewportSize({ width: 1080, height: 800 })
    await page.getByRole('button', { name: 'History', exact: true }).click()
    await expect(page.getByRole('dialog').first()).toBeVisible()
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'explore ideas', exact: true }).click()
    await expect(page.locator('[contenteditable="true"]').first()).toContainText('Help me explore ideas for')
    await page.keyboard.press('Enter')
    await expect(page.getByText(/mock inference server|boot chain is working/).and(page.locator(':visible')).first()).toBeVisible({ timeout: 60_000 })
    await page.emulateMedia({ forcedColors: 'active' })
    expect((await material()).blur).toBe('none')
  } finally {
    await fixture.cleanup()
  }
})
