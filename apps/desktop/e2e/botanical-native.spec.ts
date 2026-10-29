import { test, expect } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'
test('native hidden pauses botanical worker and both themes retain the landing at narrow widths',async({},info)=>{
 const f=await setupMockBackend()
 try {
  await waitForAppReady(f,45000)
  await f.page.locator('[data-landing-botanical]').waitFor()
  const worker=f.page.workers().find(w=>w.url().includes('landing-bloom.worker'))!
  expect(worker).toBeTruthy()
  const frames=()=>worker.evaluate(()=> (self as unknown as {__landingBloomFrames:number}).__landingBloomFrames)
  await f.page.emulateMedia({reducedMotion:'no-preference'})
  const before=await frames();await expect.poll(frames).toBeGreaterThan(before+2)
  await f.app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].hide())
  await f.page.waitForTimeout(250);const hidden=await frames();await f.page.waitForTimeout(350)
  expect(await frames()).toBe(hidden)
  await f.app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].show())
  await expect.poll(frames).toBeGreaterThan(hidden+2)
  await f.page.emulateMedia({reducedMotion:'reduce'});await f.page.waitForTimeout(250)
  const reduced=await frames();await f.page.waitForTimeout(350);expect(await frames()).toBe(reduced)
  for(const theme of ['light','dark']) {
   await f.page.evaluate(theme=>localStorage.setItem('clover-desktop-mode-v1',theme),theme)
   await f.page.reload();await waitForAppReady(f,45000)
   for(const width of [1080,375]) {
    await f.page.setViewportSize({width,height:800});await f.page.waitForTimeout(300)
    expect(await f.page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await expect(f.page.locator('[data-slot="composer-surface"]')).toBeVisible()
    await f.page.screenshot({path:info.outputPath(`${theme}-${width}.png`)})
   }
  }
 } finally {await f.cleanup()}
})
