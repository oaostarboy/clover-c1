import fs from 'node:fs'
import path from 'node:path'
import { test } from '@playwright/test'
import { setupMockBackend, waitForAppReady } from './fixtures'
import { BLOCKING_CLARIFY_TRIGGER, BLOCKING_CLARIFY_QUESTION, INTERIM_TEXTS, restartMockServer } from './mock-server'

for (const mode of ['clarify', 'on', 'off']) test(`diagnose ${mode}`, async ({}, info) => {
  restartMockServer()
  const f = await setupMockBackend(mode === 'off' ? {extraDisplayConfig:'  interim_assistant_messages: false'} : {})
  const frames: unknown[] = []
  try {
    await waitForAppReady(f, 45000)
    const cdp = await f.page.context().newCDPSession(f.page)
    await cdp.send('Network.enable')
    cdp.on('Network.webSocketFrameReceived', event => {
      try { frames.push({direction:'received', data:JSON.parse(event.response.payloadData)}) } catch {}
    })
    cdp.on('Network.webSocketFrameSent', event => {
      try { frames.push({direction:'sent', data:JSON.parse(event.response.payloadData)}) } catch {}
    })
    const composer=f.page.locator('[contenteditable="true"]').first()
    await composer.fill(mode==='clarify' ? BLOCKING_CLARIFY_TRIGGER : 'E2E_INTERIM_TRIGGER')
    await f.page.keyboard.press('Enter')
    await f.page.getByText(mode==='clarify' ? BLOCKING_CLARIFY_QUESTION : INTERIM_TEXTS.finalText,{exact:false}).first().waitFor({timeout:45000})
    if(mode==='clarify') {
      await composer.click(); await composer.type('please answer tersely',{delay:20})
    }
    await f.page.waitForTimeout(2500)
    fs.writeFileSync(info.outputPath('frames.json'),JSON.stringify(frames,null,2))
    fs.writeFileSync(info.outputPath('body.txt'),await f.page.locator('body').innerText())
    await f.page.screenshot({path:info.outputPath('surface.png')})
    for(const name of ['logs','state.db']) {
      const src=path.join(f.sandbox.cloverHome,name)
      if(fs.existsSync(src)) fs.cpSync(src,info.outputPath(name),{recursive:true})
    }
    console.log('receipt',mode,info.outputDir,'frames',frames.length)
  } finally { await f.cleanup() }
})
