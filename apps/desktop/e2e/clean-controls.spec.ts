import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

test('diagnostics and composer controls remain discoverable', async () => {
  const fixture = await setupMockBackend()
  try {
    const page = fixture.page
    await waitForAppReady(fixture, 120_000)
    await page.getByRole('button', { name: 'Open settings' }).click()
    await page.getByRole('button', { name: 'About', exact: true }).click()
    await expect(page.getByText(/Version \d+\.\d+/).first()).toBeVisible()
    await page.getByRole('button', { name: 'Close settings' }).click()
    await page.getByRole('button', { name: 'Add context' }).click()
    for (const name of ['Files…', 'Folder…', 'Images…', 'Prompt snippets…']) {
      await expect(page.getByRole('menuitem', { name })).toBeVisible()
    }
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: /Model ·/ }).click()
    await expect(page.getByRole('menuitem', { name: /Mock Model/ })).toBeVisible()
    await page.keyboard.press('Escape')
    for (const name of ['Voice dictation', 'Read replies aloud', 'Start voice conversation']) {
      await expect(page.getByRole('button', { name })).toBeVisible()
    }
  } finally {
    await fixture.cleanup()
  }
})
