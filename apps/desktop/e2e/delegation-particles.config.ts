import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: '.', testMatch: 'delegation-particles.spec.ts', workers: 1,
  outputDir: 'test-results/delegation-particles',
  use: { baseURL: 'http://127.0.0.1:5197', headless: true, launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } },
  webServer: { command: '../../node_modules/.bin/vite --host 127.0.0.1 --port 5197 --strictPort', cwd: process.cwd(), url: 'http://127.0.0.1:5197/e2e/fixtures/delegation-particles.html', reuseExistingServer: false }
})
