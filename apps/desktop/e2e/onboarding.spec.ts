/**
 * E2E onboarding tests — verify the provider picker appears when no
 * inference provider is configured.
 *
 * Launches the app with an empty config.yaml (no providers). The renderer
 * should detect the unconfigured state and show the DesktopOnboardingOverlay
 * with provider options / API key form.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test } from './test'

import {
  type NoProviderFixture,
  setupNoProvider,
  waitForOnboarding,
} from './fixtures'
import { expectVisualSnapshot } from './visual-snapshot'

let fixture: NoProviderFixture | null = null

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('onboarding with no provider configured', () => {
  test('onboarding overlay appears on first boot', async () => {
    fixture = await setupNoProvider()

    // The app should boot (clover serve starts fine even without a provider),
    // but the renderer should show the onboarding overlay because no
    // provider is configured.
    await waitForOnboarding(fixture.page, 90_000)
  })

  test('onboarding shows provider options or API key form', async () => {
    if (!fixture) {
      test.skip(true, 'Previous test failed — no app running')

      return
    }

    const page = fixture.page

    // The onboarding overlay should contain provider-related text.
    // It might show OAuth providers, an API key form, or a "choose later"
    // link. Verify at least one of these is visible.
    const rootText = await page.evaluate(() => {
      const root = document.getElementById('root')

      return root?.textContent ?? ''
    })

    const hasProviderText =
      rootText.includes('provider') ||
      rootText.includes('Provider') ||
      rootText.includes('API key') ||
      rootText.includes('Sign in') ||
      rootText.includes('OpenRouter') ||
      rootText.includes('OpenAI')

    expect(hasProviderText).toBe(true)
  })

  test('screenshot of onboarding overlay', async () => {
    if (!fixture) {
      test.skip(true, 'Previous test failed — no app running')

      return
    }

    await expectVisualSnapshot(fixture.page, { name: 'onboarding-overlay', app: fixture.app })
  })
})

test.describe('existing-config desktop tour', () => {
  test('displays desktop tour on first launch when config exists', async () => {
    if (!fixture) {
      test.skip(true, 'No fixture available')
      return
    }

    const page = fixture.page

    // Trigger the existing-config tour by seeding localStorage and reloading
    await page.evaluate(() => {
      window.localStorage.setItem('clover-desktop-onboarded-v1', '1')
      window.localStorage.removeItem('clover-desktop-tour-v1')
      window.localStorage.setItem('clover-force-tour', '1')
    })
    await page.reload()

    // Verify Desktop Tour Overlay is rendered
    await page.waitForSelector('[data-desktop-tour-overlay]', { timeout: 10_000 })
    const overlayText = await page.textContent('[data-desktop-tour-overlay]')
    expect(overlayText).toContain('Clover Desktop Superpowers')
    expect(overlayText).toContain('Chat-first sidebar & drawers')
    expect(overlayText).toContain('Config Detected:')

    // Capture visual snapshot of step 0
    await expectVisualSnapshot(page, { name: 'desktop-tour-step0', app: fixture.app })

    // Step navigation: click Continue to advance to Step 1 (Live Preview)
    const continueBtn = page.locator('[data-desktop-tour-overlay] button:has-text("Continue")')
    await continueBtn.click()
    expect(await page.textContent('[data-desktop-tour-overlay]')).toContain('Live Preview & OpenUI cards')

    // Advance to Step 2 (Terminal & Files)
    await continueBtn.click()
    expect(await page.textContent('[data-desktop-tour-overlay]')).toContain('Integrated Terminal & File Tree')

    // Advance to Step 3 (Command Center)
    await continueBtn.click()
    expect(await page.textContent('[data-desktop-tour-overlay]')).toContain('Command Center')

    // Advance to Step 4 (Completion)
    const finishBtn = page.locator('[data-desktop-tour-overlay] button:has-text("Finish Tour")')
    await finishBtn.click()
    expect(await page.textContent('[data-desktop-tour-overlay]')).toContain("You're ready for Clover Desktop!")

    // Capture visual snapshot of the tour
    await expectVisualSnapshot(page, { name: 'desktop-tour-completion', app: fixture.app })

    // Click Launch Clover Desktop to complete tour and dismiss overlay
    const launchBtn = page.locator('[data-desktop-tour-overlay] button:has-text("Launch Clover Desktop")')
    await launchBtn.click()
    await page.waitForSelector('[data-desktop-tour-overlay]', { state: 'detached', timeout: 5000 })
  })
})
