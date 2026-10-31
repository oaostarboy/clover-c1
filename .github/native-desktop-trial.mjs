import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import crypto from 'node:crypto'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { execFileSync, spawn } from 'node:child_process'
const product = path.resolve('product')
const desktop = path.join(product, 'apps/desktop')
const require = createRequire(path.join(desktop, 'package.json'))
const { _electron, expect } = require('@playwright/test')
const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => { const p = path.join(dir,e.name); return e.isDirectory() ? [p,...walk(p)] : [p] })
const baseline='f3243637efb0a2a92758eb87d98196d1a10bcc9a', target='a2c65a244315f028febaf5429089140b20c254ce', branch='candidate/drawing-2.0.2.4'
function inspectPackage(expected, phase) {
  const all=walk(path.join(desktop,'release'))
  const candidates=all.filter(p=>process.platform==='darwin'?/\.app\/Contents\/MacOS\/Clover$/.test(p):process.platform==='win32'?/win-unpacked[\\/]Clover\.exe$/i.test(p):/linux-unpacked\/clover$/i.test(p))
  const matches=candidates.map(executable=>{const stampPath=process.platform==='darwin'?path.join(executable,'../../Resources/install-stamp.json'):path.join(path.dirname(executable),'resources/install-stamp.json');return fs.existsSync(stampPath)?{executable,stamp:JSON.parse(fs.readFileSync(stampPath,'utf8'))}:null}).filter(x=>x?.stamp.commit===expected)
  assert.equal(matches.length,1,`Expected one ${phase} package, got ${matches.length}: ${candidates}`)
  assert.equal(matches[0].stamp.dirty,false)
  fs.writeFileSync(`native-${phase}-stamp.json`,JSON.stringify(matches[0].stamp,null,2))
  return matches[0]
}
let {executable,stamp}=inspectPackage(baseline,'baseline')
const sandbox = fs.mkdtempSync(path.join(os.tmpdir(),'clover-native-functional-'))
const home = path.join(sandbox,'home'), clover = path.join(home,'.clover'), userData = path.join(sandbox,'userdata')
for (const d of [home,clover,userData]) fs.mkdirSync(d,{recursive:true})
const python = path.join(product,'.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
assert.ok(fs.existsSync(python), 'Exact product Python environment missing')
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|AUTH)/i.test(k)))
// xvfb-run creates an ephemeral Xauthority file on the disposable runner.
if (process.platform === 'linux' && process.env.XAUTHORITY) env.XAUTHORITY=process.env.XAUTHORITY
Object.assign(env, {HOME:home, USERPROFILE:home, CLOVER_HOME:clover, CLOVER_DESKTOP_USER_DATA_DIR:userData, CLOVER_DESKTOP_IGNORE_EXISTING:'1', CLOVER_DESKTOP_CLOVER_ROOT:product, CLOVER_DESKTOP_CWD:home, CLOVER_DESKTOP_SKIP_QUIT_CONFIRM:'1', PYTHONPATH:product, PYTHONUNBUFFERED:'1'})
for (const k of ['CLOVER_DESKTOP_DEV_SERVER','CLOVER_DESKTOP_BOOT_FAKE','CLOVER_DESKTOP_BOOT_FAKE_STEP_MS','VIRTUAL_ENV']) delete env[k]
const result = {platform:process.platform, executable, stamp, helper:process.env.GITHUB_SHA, mode:'real app-spawned Python backend, loopback synthetic provider', phases:[], requests:[], errors:[], ok:false}
const save=()=>fs.writeFileSync('native-evidence.json',JSON.stringify(result,null,2))
const prompt='Return the native desktop test response.'
const reply='Native desktop verification complete. This saved reply must survive a full app restart.'
const sid='native-functional-preserved-session'
const py=code=>execFileSync(python,['-c',code],{cwd:product,env,encoding:'utf8',timeout:30000}).trim()
const hash=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')
const configObject=()=>JSON.parse(py("import json,yaml,os;print(json.dumps(yaml.safe_load(open(os.path.join(os.environ['CLOVER_HOME'],'config.yaml')))))"))
function preserveSubset(before,after,key=''){for(const [k,v] of Object.entries(before)){const full=key?key+'.'+k:k;if(v&&typeof v==='object'&&!Array.isArray(v)){assert.ok(after[k],full+' removed');preserveSubset(v,after[k],full)}else assert.deepEqual(after[k],v,full+' changed')}}
let app, page, provider, monitor, timeout
const quit=async()=>{const child=app.process();await app.close();assert.equal(child.exitCode,0,'App did not quit cleanly');app=null;result.phases.push('clean quit')}
const snapshot=()=>JSON.parse(py(`import sqlite3,json,os
c=sqlite3.connect(os.path.join(os.environ['CLOVER_HOME'],'state.db'))
print(json.dumps({'integrity':c.execute('PRAGMA integrity_check').fetchone()[0], 'rows':c.execute('select role,content from messages where session_id=? order by rowid',('${sid}',)).fetchall()}))`))
try {
  timeout=setTimeout(()=>{result.errors.push('Overall upgrade test deadline');save();process.exit(9)},1350000)
  provider=http.createServer((req,res)=>{
    let body='';req.on('data',c=>body+=c);req.on('end',()=>{
      const json=(code,obj)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(obj))}
      if(req.method==='GET'&&req.url==='/v1/models')return json(200,{object:'list',data:[{id:'synthetic-native',object:'model',owned_by:'fixture'}]})
      if(req.method!=='POST'||!req.url.startsWith('/v1/chat/completions'))return json(404,{error:{message:'Unsupported fixture route'}})
      let p;try{p=JSON.parse(body)}catch{return json(400,{error:{message:'Invalid JSON'}})}
      const sys=String((p.messages||[]).find(m=>m.role==='system')?.content||'')
      const user=String((p.messages||[]).filter(m=>m.role==='user').at(-1)?.content||'')
      const aux=/Size this request:|Your previous reply was invalid/.test(user)||/Reply with one JSON object only/.test(sys)
      const content=aux?'{"mode":"direct","tasks":[],"reason":"fixture plain request"}':reply
      result.requests.push({stream:!!p.stream,aux,model:p.model,tools:(p.tools||[]).length,user:user.slice(0,150)});save()
      if(!p.stream)return json(200,{id:'native',object:'chat.completion',created:0,model:'synthetic-native',choices:[{index:0,message:{role:'assistant',content},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:20,total_tokens:30}})
      res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'})
      for(const text of content.match(/.{1,12}/g)||[])res.write('data: '+JSON.stringify({id:'native',object:'chat.completion.chunk',created:0,model:'synthetic-native',choices:[{index:0,delta:{content:text},finish_reason:null}]})+'\n\n')
      res.end('data: '+JSON.stringify({id:'native',object:'chat.completion.chunk',created:0,model:'synthetic-native',choices:[{index:0,delta:{},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n')
    })
  })
  await new Promise(r=>provider.listen(0,'127.0.0.1',r))
  fs.writeFileSync(path.join(clover,'config.yaml'),`model:\n  default: synthetic-native\n  provider: synthetic\nproviders:\n  synthetic:\n    api: http://127.0.0.1:${provider.address().port}/v1\n    name: Synthetic Native Fixture\n    api_mode: chat_completions\n    key_env: SYNTHETIC_FIXTURE_KEY\n    models:\n      synthetic-native: {}\n    context_length: 65536\ntoolsets: []\nplugins:\n  enabled: []\nmcp_servers: {}\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\n`)
  fs.writeFileSync(path.join(clover,'.env'),'SYNTHETIC_FIXTURE_KEY=fixture-dummy-not-a-credential\n')
  const configBefore=hash(path.join(clover,'config.yaml')),envBefore=hash(path.join(clover,'.env'))
  const initialConfig=configObject();result.initialConfig=initialConfig
  py(`from clover_state import SessionDB
import os,time
from pathlib import Path
d=SessionDB(db_path=Path(os.environ['CLOVER_HOME'])/'state.db')
d.create_session('${sid}','desktop',cwd=os.environ['HOME'])
d.append_messages_batch('${sid}',[{'role':'user','content':'Preserved native user message','timestamp':time.time()-30},{'role':'assistant','content':'Preserved native assistant message','timestamp':time.time()-29}])
d.set_session_title('${sid}','Native preserved conversation')
d.close()`)
  result.phases.push('real SessionDB seeded')
  monitor=setInterval(()=>{const f=path.join(clover,'logs/agent.log');if(fs.existsSync(f)&&/Executing tool|tool_turns=[1-9]/.test(fs.readFileSync(f,'utf8'))){result.errors.push('Unexpected tool execution');save();process.exit(8)}},100)
  const launch=async(version)=>{
    app=await _electron.launch({executablePath:executable,args:['--disable-gpu',...(process.platform==='linux'?['--no-sandbox']:[])],env,timeout:60000})
    app.process().stdout?.on('data',d=>fs.appendFileSync('native-app.log',d))
    app.process().stderr?.on('data',d=>fs.appendFileSync('native-app.log',d))
    page=await app.firstWindow({timeout:60000});await page.waitForLoadState('domcontentloaded')
    assert.equal(await app.evaluate(({app})=>app.getVersion()),version)
    for(let i=0;i<90;i++){
      const skip=page.getByRole('button',{name:/skip for now/i});if(await skip.count())await skip.first().click().catch(()=>{})
      const tile=page.getByText('Native preserved conversation',{exact:true}).first()
      if(await tile.isVisible().catch(()=>false)){await tile.click();break}
      await page.waitForTimeout(500)
    }
    await expect(page.getByText('Preserved native assistant message',{exact:true}).first()).toBeVisible({timeout:60000})
    result.phases.push('real backend history visible');save()
  }
  await launch('2.0.2.3')
  const composer=page.locator('[contenteditable="true"]').first()
  await composer.fill(prompt);await composer.press('Enter')
  await expect(page.locator('[data-role="assistant"] [data-slot="aui_assistant-message-content"]').last()).toHaveText(reply,{timeout:60000})
  result.phases.push('exact synthetic reply rendered')
  await expect.poll(()=>snapshot().rows.at(-1),{timeout:15000}).toEqual(['assistant',reply])
  const before=snapshot();assert.equal(before.integrity,'ok');assert.ok(result.requests.some(r=>r.stream&&!r.aux))
  await quit()
  const configAfterFirstQuit=configObject();result.configAfterFirstQuit=configAfterFirstQuit
  preserveSubset(initialConfig,configAfterFirstQuit)
  const upstream=()=>execFileSync('git',['ls-remote','origin',`refs/heads/${branch}`],{cwd:product,env,encoding:'utf8',timeout:30000}).trim().split(/\s+/)[0]
  assert.equal(upstream(),target,'Candidate branch moved before update')
  const updateEnv={...env,GITHUB_SHA:target,GITHUB_REF_NAME:branch}
  result.phases.push('baseline complete; starting real updater');save()
  const updateCode=await new Promise((resolve,reject)=>{
    const child=spawn(python,['-m','clover_cli.main','update','--branch',branch,'--yes'],{cwd:product,env:updateEnv,stdio:['ignore','pipe','pipe']})
    const out=fs.createWriteStream('native-update.log');child.stdout.pipe(out,{end:false});child.stderr.pipe(out,{end:false})
    const timer=setTimeout(()=>{child.kill();reject(new Error('Real updater exceeded 20 minutes'))},1200000)
    child.on('error',reject);child.on('close',code=>{clearTimeout(timer);out.end();resolve(code)})
  })
  assert.equal(updateCode,0,'Real updater failed; see native-update.log')
  assert.equal(execFileSync('git',['rev-parse','HEAD'],{cwd:product,env,encoding:'utf8'}).trim(),target,'Updater did not install exact target')
  assert.equal(upstream(),target,'Candidate branch moved after update')
  ;({executable,stamp}=inspectPackage(target,'updated'));result.updated={executable,stamp,gitHead:target};result.phases.push('real updater completed and packaged target inspected');save()
  assert.deepEqual(snapshot(),before,'Database changed during upgrade')
  preserveSubset(initialConfig,configObject())
  await launch('2.0.2.4')
  await expect(page.getByText(prompt,{exact:true}).first()).toBeVisible({timeout:30000})
  await expect(page.locator('[data-role="assistant"] [data-slot="aui_assistant-message-content"]').last()).toHaveText(reply,{timeout:30000})
  const after=snapshot();assert.deepEqual(after,before)
  const secondPrompt='Return the updated desktop test response.'
  await page.locator('[contenteditable="true"]').first().fill(secondPrompt)
  await page.locator('[contenteditable="true"]').first().press('Enter')
  await expect(page.locator('[data-role="assistant"] [data-slot="aui_assistant-message-content"]').last()).toHaveText(reply,{timeout:60000})
  await expect.poll(()=>snapshot().rows.at(-1),{timeout:15000}).toEqual(['assistant',reply])
  const updatedRows=snapshot();result.phases.push('updated app chat succeeded')
  await quit()
  await launch('2.0.2.4')
  await expect(page.getByText(secondPrompt,{exact:true}).first()).toBeVisible({timeout:30000})
  await expect(page.locator('[data-role="assistant"] [data-slot="aui_assistant-message-content"]').last()).toHaveText(reply,{timeout:30000})
  assert.deepEqual(snapshot(),updatedRows)
  result.phases.push('updated app restart preserved new chat')
  result.persistence={messages:after.rows.length,integrity:after.integrity,logicalRowsUnchanged:true,fullPriorReplyVisible:true,configUnchanged:hash(path.join(clover,'config.yaml'))===configBefore,envUnchanged:hash(path.join(clover,'.env'))===envBefore,uiSettingToggled:false}
  result.configAfterRestart=configObject()
  preserveSubset(initialConfig,result.configAfterRestart)
  assert.deepEqual(result.configAfterRestart,configAfterFirstQuit,'Config changed across restart')
  result.persistence.originalConfigValuesPreserved=true
  result.persistence.configStableAcrossRestart=true
  assert.ok(result.persistence.envUnchanged)
  await quit()
  const log=fs.readFileSync(path.join(clover,'logs/agent.log'),'utf8')
  const turns=[...log.matchAll(/tool_turns=(\d+)/g)].map(m=>Number(m[1]));assert.ok(turns.length>0);assert.ok(turns.every(n=>n===0))
  result.toolTurns=turns;result.ok=true
} catch(e) {
  result.errors.push(e.stack||String(e))
  if(page) {try{result.failureText=(await page.locator('body').innerText()).slice(0,5000);await page.screenshot({path:'native-failure.png'})}catch{}}
} finally {
  clearInterval(monitor);clearTimeout(timeout)
  if(app)try{await app.close()}catch(e){result.errors.push('Cleanup: '+String(e))}
  if(provider){provider.closeAllConnections();await new Promise(r=>provider.close(r))}
  const agent=path.join(clover,'logs/agent.log');if(fs.existsSync(agent))fs.copyFileSync(agent,'native-agent.log')
  result.ok=result.ok&&result.errors.length===0
  save();console.log(JSON.stringify(result,null,2))
}
if(!result.ok)process.exitCode=1
