import fs from 'node:fs'
import path from 'node:path'
import {test,expect} from './test'
import {createSandbox,writeMockProviderConfig,writeEnvFile,buildAppEnv,launchDesktop,waitForAppReady} from './fixtures'
import {startMockServer} from './mock-server'
test('real standalone consumer reaches the official activity adapter and registered comparison renderer',async({},info)=>{
 const sandbox=createSandbox('comparison')
 const mock=await startMockServer({comparisonCard:{version:1,title:'Authored integration comparison '+ 'x'.repeat(110),columns:['Option A','Option B'],rows:[{label:'Rendering',values:['<script>not code</script>','Plain text']}]}})
 fs.mkdirSync(path.join(sandbox.cloverHome,'plugins'),{recursive:true})
 fs.cpSync(path.resolve(import.meta.dirname,'../../../integrations/generative-ui/consumer'),path.join(sandbox.cloverHome,'plugins/comparison-card'),{recursive:true})
 writeMockProviderConfig(sandbox.cloverHome,mock.url,'  tool_progress: off','plugins:\n  enabled: [comparison-card]\ndelegation:\n  router:\n    enabled: false')
 writeEnvFile(sandbox.cloverHome)
 const {app,page}=await launchDesktop(buildAppEnv(sandbox,{CLOVER_TUI_TOOLSETS:'comparison'}))
 const cdp=await page.context().newCDPSession(page);await cdp.send('Network.enable');await cdp.send('Emulation.setFocusEmulationEnabled',{enabled:true})
 const frames:unknown[]=[];cdp.on('Network.webSocketFrameReceived',e=>{try{frames.push({received:JSON.parse(e.response.payloadData)})}catch{}})
 cdp.on('Network.webSocketFrameSent',e=>{try{frames.push({sent:JSON.parse(e.response.payloadData)})}catch{}})
 const fixture={app,page,mock,mockUrl:mock.url,sandbox,cleanup:async()=>{await app.close();await mock.close();sandbox.cleanup()}}
 try {
  await waitForAppReady(fixture,45000)
  const transcript=page.locator('[data-slot="aui_thread-viewport"]')
  await page.locator('[contenteditable="true"]').first().fill('E2E_COMPARISON_TRIGGER')
  await page.keyboard.press('Enter')
  const card=page.locator('[data-registered-comparison]')
  await expect(card).toBeVisible({timeout:45000})
  await expect(card.getByText('Complete',{exact:true})).toBeVisible({timeout:15000})
  await expect(card.getByText('<script>not code</script>',{exact:true})).toBeVisible()
  expect(await card.locator('script,iframe,img,a').count()).toBe(0)
  await card.getByRole('button',{name:'List view'}).click()
  await expect(card.getByRole('button',{name:'List view'})).toHaveAttribute('aria-pressed','true')
  await page.screenshot({path:info.outputPath('comparison-live-native.png')})
  await page.reload();await waitForAppReady(fixture,45000)
  await expect(page.locator('[data-registered-comparison]')).toBeVisible({timeout:20000})
  await expect(page.locator('[data-registered-comparison]').getByText('Complete',{exact:true})).toBeVisible()
  await page.screenshot({path:info.outputPath('comparison-reopened.png')})
  await expect(transcript).toContainText('mock inference server')
  const visual=[]
  for(const theme of ['light','dark']){
   await page.evaluate(theme=>localStorage.setItem('clover-desktop-mode-v1',theme),theme)
   await page.reload();await waitForAppReady(fixture,45000);await expect(card).toBeVisible()
   await page.setViewportSize({width:375,height:830})
   await card.getByRole('button',{name:'Table view'}).focus();await page.keyboard.press('Tab')
   await expect(card.getByRole('button',{name:'List view'})).toBeFocused();await page.keyboard.press('Space')
   await expect(card.getByRole('button',{name:'List view'})).toHaveAttribute('aria-pressed','true')
   await page.keyboard.press('Shift+Tab');await expect(card.getByRole('button',{name:'Table view'})).toBeFocused();await page.keyboard.press('Enter')
   await expect(card.getByRole('button',{name:'Table view'})).toHaveAttribute('aria-pressed','true')
   await page.waitForTimeout(200)
   const styles=await card.evaluate(el=>{
    const rgba=(color:string)=>{const c=document.createElement('canvas');c.width=c.height=1;const ctx=c.getContext('2d')!;ctx.fillStyle=color;ctx.fillRect(0,0,1,1);return Array.from(ctx.getImageData(0,0,1,1).data)}
    let background=[255,255,255],chain:Element[]=[];for(let node:Element|null=el;node;node=node.parentElement)chain.unshift(node)
    for(const node of chain){const c=rgba(getComputedStyle(node).backgroundColor),alpha=c[3]/255;background=background.map((x,i)=>x*(1-alpha)+c[i]*alpha)}
    const luminance=(c:number[])=>c.slice(0,3).map(x=>{x/=255;return x<=.04045?x/12.92:Math.pow((x+.055)/1.055,2.4)}).reduce((s,x,i)=>s+x*[.2126,.7152,.0722][i],0)
    return {hasFocus:document.hasFocus(),active:document.activeElement?.textContent,background,text:Array.from(el.querySelectorAll('h3,p,th,td,button')).map(node=>{const color=rgba(getComputedStyle(node).color),effective=color.slice(0,3).map((x,i)=>x*color[3]/255+background[i]*(1-color[3]/255)),a=luminance(effective),b=luminance(background);return {label:node.textContent,color,contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)}}),buttons:Array.from(el.querySelectorAll('button')).map(b=>({height:b.getBoundingClientRect().height,focus:getComputedStyle(b).boxShadow,focusVisible:b.matches(':focus-visible'),outline:getComputedStyle(b).outline,outlineWidth:getComputedStyle(b).outlineWidth,outlineOffset:getComputedStyle(b).outlineOffset,outlineStyle:getComputedStyle(b).outlineStyle,ring:getComputedStyle(b).getPropertyValue('--ring'),border:getComputedStyle(b).border,ringShadow:getComputedStyle(b).getPropertyValue('--tw-ring-shadow')})),overflow:document.documentElement.scrollWidth>innerWidth}
   })
   fs.writeFileSync(info.outputPath(`contrast-${theme}.json`),JSON.stringify(styles,null,2))
   expect(styles.overflow).toBe(false);expect(styles.buttons.every(b=>b.height>=44)).toBe(true);expect(styles.text.every(t=>t.contrast>=4.5)).toBe(true)
   visual.push({theme,...styles});fs.writeFileSync(info.outputPath('focus-probe.json'),JSON.stringify(visual,null,2));expect(styles.hasFocus).toBe(true);expect(styles.buttons.some(b=>b.focusVisible&&(/0px 0px 0px [1-9]/.test(b.focus)||(b.outlineStyle!=='none'&&parseFloat(b.outlineWidth)>=1&&parseFloat(b.outlineOffset)<=0)))).toBe(true);await page.screenshot({path:info.outputPath(`comparison-${theme}-375.png`)})
  }
  fs.writeFileSync(info.outputPath('contrast-keyboard.json'),JSON.stringify(visual,null,2))
  fs.cpSync(path.join(sandbox.cloverHome,'logs'),info.outputPath('backend-logs'),{recursive:true})
 } finally {
  fs.writeFileSync(info.outputPath('frames.json'),JSON.stringify(frames,null,2))
  fs.writeFileSync(info.outputPath('tool-dom.txt'),await page.locator('[data-slot="aui_thread-viewport"]').innerHTML())
  for(const name of ['state.db','state.db-wal','state.db-shm'])if(fs.existsSync(path.join(sandbox.cloverHome,name)))fs.copyFileSync(path.join(sandbox.cloverHome,name),info.outputPath(name))
  fs.cpSync(path.join(sandbox.cloverHome,'logs'),info.outputPath('backend-logs'),{recursive:true});await fixture.cleanup()
 }
})
