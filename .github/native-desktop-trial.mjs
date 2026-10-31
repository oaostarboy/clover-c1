import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
const product = path.resolve('product')
const desktop = path.join(product, 'apps/desktop')
const require = createRequire(path.join(desktop, 'package.json'))
const { _electron } = require('@playwright/test')
const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => { const p = path.join(dir,e.name); return e.isDirectory() ? [p,...walk(p)] : [p] })
const all = walk(path.join(desktop, 'release'))
const executable = process.platform === 'darwin' ? all.find(p => /\.app\/Contents\/MacOS\/Clover$/.test(p)) : all.find(p => /win-unpacked[\\/]Clover\.exe$/i.test(p))
if (!executable) throw new Error('Packaged executable not found')
const stampPath = process.platform === 'darwin' ? path.join(executable, '../../Resources/install-stamp.json') : path.join(path.dirname(executable), 'resources/install-stamp.json')
const stamp = JSON.parse(fs.readFileSync(stampPath, 'utf8'))
if (stamp.commit !== '7c43ea32629ceff116a047515f1185890ccf081f' || stamp.dirty !== false) throw new Error('Incorrect embedded stamp')
const sandbox = fs.mkdtempSync(path.join(os.tmpdir(),'clover-native-trial-'))
const home = path.join(sandbox,'home'), clover = path.join(home,'.clover'), userData = path.join(sandbox,'userdata')
for (const d of [home,clover,userData]) fs.mkdirSync(d,{recursive:true})
fs.writeFileSync(path.join(clover,'config.yaml'), '# isolated native trial\n')
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|AUTH)/i.test(k)))
Object.assign(env, {HOME:home, USERPROFILE:home, CLOVER_HOME:clover, CLOVER_DESKTOP_USER_DATA_DIR:userData, CLOVER_DESKTOP_IGNORE_EXISTING:'1', CLOVER_DESKTOP_BOOT_FAKE:'1', CLOVER_DESKTOP_BOOT_FAKE_STEP_MS:'10', CLOVER_DESKTOP_SKIP_QUIT_CONFIRM:'1'})
delete env.CLOVER_DESKTOP_DEV_SERVER
const app = await _electron.launch({ executablePath:executable, args:['--disable-gpu'], env, timeout:60000 })
try {
  const page = await app.firstWindow({timeout:60000})
  await page.waitForLoadState('domcontentloaded')
  await page.waitForFunction(() => document.body && document.body.innerText.trim().length > 0, null, {timeout:60000})
  const version = await app.evaluate(({app}) => app.getVersion())
  if (version !== '2.0.2.4') throw new Error(`Wrong app version: ${version}`)
  const text = (await page.locator('body').innerText()).slice(0,1000)
  const result = {platform:process.platform, executable, stamp, version, rendererText:text, helper:process.env.GITHUB_SHA, cleanQuit:false}
  await app.close()
  result.cleanQuit = true
  fs.writeFileSync('native-evidence.json', JSON.stringify(result,null,2))
  console.log(JSON.stringify(result,null,2))
} catch (e) { await app.close().catch(()=>{}); throw e }
