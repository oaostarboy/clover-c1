import fs from 'node:fs'
import path from 'node:path'
import { test, expect } from './test'
import { createSandbox, writeMockProviderConfig, writeEnvFile, buildAppEnv, launchDesktop, waitForAppReady } from './fixtures'
import { startMockServer } from './mock-server'

const OPENUI_DSL = `title = "Model Architecture: Claude 3.7 vs GPT-4.5"
cols = ["Claude 3.7 Sonnet", "GPT-4.5 Research"]
rows = [
  Row("Reasoning Engine", ["Hybrid (Fast & Extended)", "Unified Research Engine"]),
  Row("Intelligent UI", ["OpenUI Lang Native Cards", "Raw JSON & Code Blocks"]),
  Row("Token Overhead", ["67% reduction vs AST", "Baseline streaming"]),
  Row("Safety & Validation", ["Closed Bounded Schema", "Dynamic HTML eval"])
]
root = Comparison(title, cols, rows)`

test('renders an actual OpenUI / Intelligent UI comparison card inside frosted desktop chat UI', async ({}, info) => {
  const sandbox = createSandbox('openui-demo')
  const mock = await startMockServer({
    comparisonCard: {
      _toolName: 'openui_card',
      version: 1,
      title: 'Intelligent UI: Claude 3.7 Sonnet vs GPT-4.5 Research',
      columns: ['Claude 3.7 Sonnet', 'GPT-4.5 Research'],
      rows: [
        { label: 'Reasoning Engine', values: ['Hybrid (Fast & Extended)', 'Unified Research Engine'] },
        { label: 'Intelligent UI', values: ['OpenUI Lang Native Cards', 'Raw JSON & Code Blocks'] },
        { label: 'Token Overhead', values: ['67% reduction vs AST', 'Baseline streaming'] },
        { label: 'Safety & Validation', values: ['Closed Bounded Schema', 'Dynamic HTML eval'] },
      ],
      openui_lang: OPENUI_DSL,
    },
  })

  fs.mkdirSync(path.join(sandbox.cloverHome, 'plugins'), { recursive: true })
  fs.cpSync(
    path.resolve(import.meta.dirname, '../../../integrations/generative-ui/openui-card'),
    path.join(sandbox.cloverHome, 'plugins/openui-card'),
    { recursive: true }
  )
  writeMockProviderConfig(
    sandbox.cloverHome,
    mock.url,
    '  tool_progress: off',
    'plugins:\n  enabled: [openui-card]\ndelegation:\n  router:\n    enabled: false'
  )
  writeEnvFile(sandbox.cloverHome)

  const { app, page } = await launchDesktop(
    buildAppEnv(sandbox, { CLOVER_TUI_TOOLSETS: 'openui' })
  )
  const fixture = { app, page, mock, mockUrl: mock.url, sandbox, cleanup: async () => { await app.close(); await mock.close(); sandbox.cleanup() } }

  try {
    await waitForAppReady(fixture, 45000)
    await page.setViewportSize({ width: 1200, height: 860 })

    const composer = page.locator('[contenteditable="true"]').first()
    await composer.fill('E2E_COMPARISON_TRIGGER Compare Claude 3.7 Sonnet and GPT-4.5 Research using Intelligent UI')
    await page.keyboard.press('Enter')

    const card = page.locator('[data-registered-comparison]')
    await expect(card).toBeVisible({ timeout: 45000 })
    await expect(card.getByText('Complete', { exact: true })).toBeVisible({ timeout: 15000 })

    // Verify OpenUI badge and buttons
    await expect(card.locator('span', { hasText: 'OpenUI' })).toBeVisible()
    await expect(card.getByRole('button', { name: 'Table view' })).toBeVisible()
    await expect(card.getByRole('button', { name: 'List view' })).toBeVisible()
    await expect(card.getByRole('button', { name: 'OpenUI Lang source view' })).toBeVisible()

    // Wait a brief moment for frost and layout to settle
    await page.waitForTimeout(500)

    const screenshotPath = '/home/starboy/Projects/clover-desktop-work/openui-integration/intelligent-ui-demo.png'
    await page.screenshot({ path: screenshotPath })
    await page.screenshot({ path: info.outputPath('intelligent-ui-demo.png') })

    // Test Lang view toggle
    await card.getByRole('button', { name: 'OpenUI Lang source view' }).click()
    await expect(card.getByRole('button', { name: 'OpenUI Lang source view' })).toHaveAttribute('aria-pressed', 'true')
    await expect(card.locator('pre code')).toContainText('root = Comparison(title, cols, rows)')

    // Switch back to Table view
    await card.getByRole('button', { name: 'Table view' }).click()
    await expect(card.getByRole('button', { name: 'Table view' })).toHaveAttribute('aria-pressed', 'true')

  } finally {
    await fixture.cleanup()
  }
})
