#!/usr/bin/env node
/* global document */
// Run inside an isolated Linux compositor with CLOVER_TEST_VIDEO=<local mp4>.
// Exercises Chromium's actual native video control, not a synthetic DOM button.
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { _electron, expect } from '@playwright/test'
import { build } from 'esbuild'

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const temp = mkdtempSync(path.join(tmpdir(), 'clover-fullscreen-'))
let app

try {
  assert.ok(process.env.CLOVER_TEST_VIDEO, 'CLOVER_TEST_VIDEO is required')
  const permissionModule = path.join(temp, 'permissions.cjs')
  await build({
    entryPoints: [path.join(desktop, 'electron/media-permissions.ts')],
    outfile: permissionModule,
    bundle: true,
    platform: 'node',
    format: 'cjs'
  })
  app = await _electron.launch({
    args: ['--no-sandbox', '--ozone-platform=x11', path.join(desktop, 'electron/media-fullscreen-fixture/main.cjs')],
    env: { ...process.env, CLOVER_TEST_PERMISSION_MODULE: permissionModule }
  })
  const page = await app.firstWindow()
  const nativeState = () =>
    app.evaluate(({ BrowserWindow }) => {
      const win = BrowserWindow.getAllWindows()[0]
      return { maximized: win.isMaximized(), fullscreen: win.isFullScreen(), bounds: win.getBounds() }
    })
  await page.locator('video').waitFor()
  await page.waitForFunction(() => document.querySelector('video').readyState >= 2)
  for (const scenario of [
    { playing: false, maximized: false },
    { playing: true, maximized: false },
    { playing: true, maximized: true }
  ]) {
    if (scenario.maximized) {
      await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].maximize())
      await expect.poll(async () => (await nativeState()).maximized).toBe(true)
    }
    await page.evaluate(async playing => {
      const video = document.querySelector('video')
      video.pause()
      video.currentTime = 1
      await new Promise(resolve => video.addEventListener('seeked', resolve, { once: true }))
      if (playing) {
        await video.play()
      }
    }, scenario.playing)
    const before = await nativeState()
    const cdp = await page.context().newCDPSession(page)
    await expect
      .poll(
        async () => {
          const state = await cdp.send('Runtime.evaluate', {
            expression: 'navigator.userActivation.isActive',
            userGesture: false,
            returnByValue: true
          })
          return state.result.value
        },
        { timeout: 10_000 }
      )
      .toBe(false)
    const automaticResult = await cdp.send('Runtime.evaluate', {
      expression: "document.querySelector('video').requestFullscreen().then(() => 'allowed', () => 'denied')",
      userGesture: false,
      awaitPromise: true,
      returnByValue: true
    })
    const automatic = automaticResult.result.value
    assert.equal(automatic, 'denied', 'fullscreen without a user gesture must fail')
    const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true })
    const buttonIds = []
    const walk = node => {
      const attributes = (node.attributes ?? []).join(' ')
      if (attributes.includes('webkit-media-controls-fullscreen-button') && !attributes.includes('display: none')) {
        buttonIds.push(node.nodeId)
      }
      for (const child of [...(node.children ?? []), ...(node.shadowRoots ?? [])]) {
        walk(child)
      }
    }
    walk(root)
    assert.ok(buttonIds.length, 'native fullscreen control exists')
    const videoBox = await page.locator('video').boundingBox()
    await page.mouse.move(videoBox.x + videoBox.width - 20, videoBox.y + videoBox.height - 20)
    let box
    await expect
      .poll(async () => {
        for (const nodeId of buttonIds) {
          try {
            const { model } = await cdp.send('DOM.getBoxModel', { nodeId })
            if (model.width && model.height) {
              box = model.border
              return true
            }
          } catch {
            /* hidden duplicate control */
          }
        }
        return false
      })
      .toBe(true)
    await page.mouse.click((box[0] + box[4]) / 2, (box[1] + box[5]) / 2)
    await page.waitForFunction(() => document.fullscreenElement?.tagName === 'VIDEO')
    await expect.poll(async () => (await nativeState()).fullscreen).toBe(true)
    const entered = await page.evaluate(() => ({
      paused: document.querySelector('video').paused,
      time: document.querySelector('video').currentTime
    }))
    assert.equal(entered.paused, !scenario.playing)
    if (scenario.playing) {
      await page.waitForFunction(time => document.querySelector('video').currentTime > time + 0.2, entered.time)
    }
    await page.evaluate(() => document.exitFullscreen())
    await page.waitForFunction(() => !document.fullscreenElement)
    await expect.poll(async () => (await nativeState()).fullscreen).toBe(false)
    await expect.poll(async () => (await nativeState()).maximized).toBe(before.maximized)
    const after = await nativeState()
    const exited = await page.evaluate(() => ({
      paused: document.querySelector('video').paused,
      time: document.querySelector('video').currentTime
    }))
    assert.equal(exited.paused, !scenario.playing)
    if (!scenario.playing) {
      assert.ok(Math.abs(exited.time - entered.time) < 0.1)
    }
    assert.deepEqual(after.bounds, before.bounds, 'Fullscreen exit restores prior window geometry')
    console.log(
      JSON.stringify({
        scenario,
        automatic,
        before,
        entered,
        after,
        exited,
        exit_method: 'document.exitFullscreen',
        passed: true
      })
    )
    await cdp.detach()
  }
} finally {
  await app?.close()
  rmSync(temp, { recursive: true, force: true })
}
