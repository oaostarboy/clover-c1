import { expect, test, allowErrorBanners } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'

test('connection trouble offers recovery and clears after reconnection', async () => {
  allowErrorBanners()
  const fixture = await setupMockBackend()
  try {
    const page = fixture.page
    // Track only sockets created by this test's isolated renderer.
    await page.addInitScript(() => {
      const NativeSocket = window.WebSocket
      const sockets: WebSocket[] = []
      class TrackedSocket extends NativeSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols)
          sockets.push(this)
        }
      }
      window.WebSocket = TrackedSocket
      ;(window as unknown as { testSockets: WebSocket[] }).testSockets = sockets
    })
    await page.reload()
    await waitForAppReady(fixture, 120_000)
    await expect(page.locator('[data-slot="statusbar"]').getByText('ready')).toHaveCount(0)
    await page.waitForFunction(() => (window as unknown as { testSockets: WebSocket[] }).testSockets.some(socket => socket.readyState === WebSocket.OPEN))
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Network.enable')
    await cdp.send('Network.emulateNetworkConditions', {
      offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0
    })
    await page.evaluate(() => {
      (window as unknown as { testSockets: WebSocket[] }).testSockets.forEach(socket => socket.close())
    })
    const gateway = page.locator('[data-slot="statusbar"]').getByRole('button', { name: /gateway\s*(offline|connecting)/i })
    await expect(gateway).toBeVisible({ timeout: 20_000 })
    await gateway.click()
    const reconnect = page.getByRole('button', { name: 'Reconnect gateway' })
    await expect(reconnect).toBeVisible()
    await reconnect.click()
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1
    })
    await expect(gateway).toHaveCount(0, { timeout: 30_000 })
  } finally {
    await fixture.cleanup()
  }
})
