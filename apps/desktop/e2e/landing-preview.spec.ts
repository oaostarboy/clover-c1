import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

test('compact navigation and fresh chat', async () => {
  const fixture = await setupMockBackend()
  try {
    await waitForAppReady(fixture, 120_000)
    const page = fixture.page
    await page.evaluate(() => localStorage.setItem('clover-desktop-mode-v1', 'dark'))
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await expect(page.locator('[data-landing]')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('[data-slot="statusbar"]').getByText('Gateway ready')).toHaveCount(0)
    // Idle versions are hidden, but an actionable update or its progress must remain visible.
    const versionLabels = page.locator('[data-slot="statusbar"]').getByText(/^v\d+\.\d+/)
    for (const label of await versionLabels.all()) {
      await expect(label).toContainText(/\(\+\d+\)|update|restart/i)
    }
    await expect(page.locator('[data-slot="composer-surface"]')).toBeVisible()
    const headlines = readFileSync(resolve(import.meta.dirname, '../src/components/chat/intro-copy.jsonl'), 'utf8')
      .split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line) as { personality: string; headline: string })
      .filter(copy => copy.personality === 'none').map(copy => copy.headline)
    for (const width of [1080, 600, 375]) {
      await page.setViewportSize({ width, height: 800 })
      const failures = await page.locator('[data-slot="aui_intro"] h1').evaluate((el, copies) => {
        const old = el.textContent
        const composer = document.querySelector('[data-slot="composer-surface"]')!
        const failures: string[] = []
        for (const copy of copies) {
          el.textContent = copy
          if (el.scrollWidth > el.clientWidth || el.getBoundingClientRect().bottom > composer.getBoundingClientRect().top + 2) failures.push(copy)
        }
        el.textContent = old
        return failures
      }, headlines)
      expect(failures, `headline overflow at ${width}px`).toEqual([])
    }
    await page.setViewportSize({ width: 1080, height: 800 })
    const track = page.locator('[data-tree-panes~="sessions"]').first()
    const rail = page.locator('[data-compact-navigation]')
    const toggle = page.getByRole('button', { name: 'Sessions and projects' })
    await expect(toggle).toBeVisible()
    expect(await track.evaluate(el => el.getBoundingClientRect().width)).toBe(54)
    const mixedMaxWidth = await track.evaluate(el => {
      const original = el.getAttribute('data-tree-panes')!
      el.setAttribute('data-tree-panes', 'sessions custom:pane')
      const width = getComputedStyle(el).maxWidth
      el.setAttribute('data-tree-panes', original)
      return width
    })
    expect(mixedMaxWidth).not.toBe('54px')
    await expect(page.getByText(/Good morning|Good afternoon|Good evening/, { exact: true })).toHaveCount(0)
    await expect(rail.getByRole('button', { name: 'Capabilities', exact: true })).toHaveCount(0)
    await rail.getByRole('button', { name: 'More tools and channels' }).click()
    for (const name of ['New session', 'Capabilities', 'Messaging', 'Artifacts', 'Scheduled jobs']) {
      const button = rail.getByRole('button', { name, exact: true })
      await expect(button).toBeVisible()
      await expect(button.locator(':scope > :first-child')).toBeVisible()
    }
    await rail.getByRole('button', { name: 'More tools and channels' }).click()
    await page.getByRole('button', { name: 'Collapse navigation' }).click()
    const botanical = page.locator('[data-landing-botanical]')
    await expect(botanical).toBeVisible()
    // Let the navigation transition and asynchronous worker resize settle.
    await page.waitForTimeout(700)
    const still = await botanical.evaluate((el: HTMLCanvasElement) => el.toDataURL())
    await page.waitForTimeout(150)
    expect(await botanical.evaluate((el: HTMLCanvasElement) => el.toDataURL())).toBe(still)
    const introBox = await page.locator('[data-slot="aui_intro"] h1').boundingBox()
    const composerBox = await page.locator('[data-slot="composer-surface"]').boundingBox()
    expect(introBox!.y + introBox!.height).toBeLessThanOrEqual(composerBox!.y + 2)
    await page.screenshot({ path: process.env.CLOVER_LAYOUT_PROOF ?? '/tmp/clover-landing-preview.png', animations: 'disabled' })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.waitForTimeout(200)
    expect(await botanical.evaluate((el: HTMLCanvasElement) => el.toDataURL())).not.toBe(still)
    if (process.env.CLOVER_MOTION_PROOF) {
      for (let frame = 0; frame < 40; frame++) {
        await page.screenshot({ path: `${process.env.CLOVER_MOTION_PROOF}/${String(frame).padStart(3, '0')}.png` })
        await page.waitForTimeout(80)
      }
    }
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await toggle.click()
    await expect(page.getByRole('button', { name: 'Collapse navigation' })).toHaveAttribute('aria-expanded', 'true')
    const expandedWidth = await track.evaluate(el => el.getBoundingClientRect().width)
    expect(expandedWidth).toBeGreaterThan(100)
    await expect(page.getByText('Bots', { exact: true }).first()).toBeVisible()
    await page.getByRole('button', { name: 'Collapse navigation' }).click()
    expect(await track.evaluate(el => el.getBoundingClientRect().width)).toBe(54)
    await page.keyboard.press('Control+k')
    await expect(page.getByRole('dialog').first()).toBeVisible()
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'History' }).click()
    await expect(page.getByRole('dialog').first()).toBeVisible()
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'explore ideas' }).click()
    await expect(page.locator('[contenteditable="true"]').first()).toContainText('Help me explore ideas for')
    await page.keyboard.press('Enter')
    await expect(page.getByText(/mock inference server|boot chain is working/).and(page.locator(':visible')).first()).toBeVisible({ timeout: 60_000 })
    await expect.poll(() => page.workers().filter(worker => worker.url().includes('landing-bloom.worker')).length).toBe(0)
    await page.keyboard.press('Control+Shift+f')
    await expect(page.getByRole('button', { name: 'Collapse navigation' })).toBeVisible()
    await expect(page.getByRole('textbox', { name: /search sessions/i })).toBeFocused()
    expect(await track.evaluate(el => el.getBoundingClientRect().width)).toBe(expandedWidth)
    await page.setViewportSize({ width: 600, height: 700 })
    await expect(page.locator('[data-chat-surface]').first()).toBeVisible()
    console.log('narrow viewport', await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth })))
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(600)
    await page.getByRole('button', { name: 'Hide sidebar' }).click()
    await expect(page.getByRole('button', { name: 'Capabilities', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'More tools and channels' }).click()
    await expect(page.getByRole('button', { name: 'Capabilities', exact: true }).first()).toBeVisible()
    await expect(page.locator('[data-narrow-overlay-tab="clover-bots:pane"]')).toBeVisible()
    await page.setViewportSize({ width: 1080, height: 800 })
    await page.evaluate(() => localStorage.setItem('clover.desktop.layoutTree.v2', JSON.stringify({
      type: 'split', id: 'custom-root', orientation: 'row', weights: [1, 3], children: [
        { type: 'group', id: 'custom-mixed', panes: ['sessions', 'files'], active: 'sessions' },
        { type: 'group', id: 'custom-workspace', panes: ['workspace'], active: 'workspace' }
      ]
    })))
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    const mixedTrack = page.locator('[data-tree-panes~="sessions"][data-tree-panes~="files"]')
    await expect(mixedTrack).toBeVisible()
    expect(await mixedTrack.evaluate(el => el.getBoundingClientRect().width)).toBeGreaterThan(100)
    await expect(mixedTrack.getByRole('button', { name: 'Capabilities', exact: true })).toHaveCount(0)
    await mixedTrack.getByRole('button', { name: 'More tools and channels' }).click()
    await expect(mixedTrack.getByRole('button', { name: 'Capabilities', exact: true })).toBeVisible()
  } finally {
    await fixture.cleanup()
  }
})

test('scheduled jobs empty state is not duplicated', async () => {
  const fixture = await setupMockBackend()
  try {
    await waitForAppReady(fixture, 120_000)
    await fixture.page.getByRole('button', { name: 'More tools and channels' }).click()
    await fixture.page.getByRole('button', { name: 'Scheduled jobs', exact: true }).first().click()
    await expect(fixture.page.getByText('No scheduled jobs yet', { exact: true })).toHaveCount(1)
  } finally {
    await fixture.cleanup()
  }
})
