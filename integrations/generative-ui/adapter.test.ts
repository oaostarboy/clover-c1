import assert from 'node:assert/strict'
import test from 'node:test'
import { AbstractAgent } from '@ag-ui/client'
import { ActivitySnapshotEventSchema } from '@ag-ui/core/schemas'
const module = await import('./adapter.ts').catch(()=>({})) as any
const record = {contract:'clover.structured-tool/1',identity:{stored_session_id:'canonical',assistant_row_id:3,tool_call_id:'call-1',turn_row_id:2},component:'clover.comparison',version:1,tool_name:'comparison_card',revision:1,status:'running',payload:{version:1,title:'Authored test comparison',columns:['A','B'],rows:[{label:'Cost',values:['Low','High']}]}}
test('uses the official client and canonical activity identity, with absorbing terminals',async()=>{
 assert.equal(typeof module.CloverActivityAgent,'function','official adapter is missing')
 let interrupts=0
 const agent=new module.CloverActivityAgent(record,()=>{interrupts++})
 assert.ok(agent instanceof AbstractAgent)
 const events:any[]=[]
 const pending=agent.connectAgent({runId:'canonical:2'},{onActivitySnapshotEvent:({event}:any)=>{ActivitySnapshotEventSchema.parse(event);events.push(event)}})
 await new Promise(r=>setTimeout(r,0))
 assert.equal(agent.accept({...record,revision:2,status:'cancelled',terminal_row_id:3}),true)
 assert.equal(agent.accept(record),false)
 assert.equal(agent.accept({...record,revision:2,status:'success',terminal_row_id:4}),false)
 await new Promise(r=>setTimeout(r,0))
 assert.equal(events.at(-1).content.status,'cancelled')
 assert.equal(events.at(-1).messageId,JSON.stringify(record.identity))
 agent.abortRun();await pending.catch(()=>{});assert.equal(interrupts,1)
})
test('fails closed on unsupported, oversized, and executable payloads',()=>{
 assert.equal(typeof module.validateRecord,'function','native record validation is missing')
 assert.ok(module.validateRecord(record))
 for(const bad of [{...record,component:'unknown'},{...record,payload:{...record.payload,html:'<script>bad</script>'}},{...record,payload:{...record.payload,title:'x'.repeat(161)}},{...record,identity:{...record.identity,assistant_row_id:0}},{...record,raw_arguments:'secret'}]) assert.equal(module.validateRecord(bad),null)
})
