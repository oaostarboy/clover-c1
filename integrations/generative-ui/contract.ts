export interface ComparisonPayload { version: 1; title: string; columns: string[]; rows: { label: string; values: string[] }[]; openui_lang?: string }
export interface NativeRecord {
 contract: 'clover.structured-tool/1'
 identity: {stored_session_id:string;assistant_row_id:number;tool_call_id:string;turn_row_id:number}
 component:'clover.comparison'|'clover.openui.comparison';version:1;tool_name:'comparison_card'|'openui_card';revision:1|2
 status:'running'|'success'|'error'|'cancelled'|'obsolete';payload:ComparisonPayload;terminal_row_id?:number
}
function closed(v:unknown,keys:string[]): v is Record<string,unknown> {
 return !!v && typeof v==='object' && !Array.isArray(v) && Object.keys(v).every(k=>keys.includes(k))
}
const text=(v:unknown,min:number,max:number):v is string=>typeof v==='string'&&v.length>=min&&v.length<=max&&!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(v)
const id=(v:unknown)=>Number.isSafeInteger(v)&&Number(v)>0
export function validateRecord(value:unknown): NativeRecord|null {
 try {
  if(!closed(value,['contract','identity','component','version','tool_name','revision','status','payload','terminal_row_id']))return null
  if(new TextEncoder().encode(JSON.stringify(value)).length>32768)return null
  if(value.contract!=='clover.structured-tool/1'||(value.component!=='clover.comparison'&&value.component!=='clover.openui.comparison')||value.version!==1||(value.tool_name!=='comparison_card'&&value.tool_name!=='openui_card'))return null
  const identity=value.identity
  if(!closed(identity,['stored_session_id','assistant_row_id','tool_call_id','turn_row_id'])||!text(identity.stored_session_id,1,512)||!text(identity.tool_call_id,1,512)||!id(identity.assistant_row_id)||!id(identity.turn_row_id))return null
  if(!['running','success','error','cancelled','obsolete'].includes(String(value.status))||value.revision!==(value.status==='running'?1:2))return null
  if(value.terminal_row_id!==undefined&&!id(value.terminal_row_id))return null
  const p=value.payload
  if(!closed(p,['version','title','columns','rows','openui_lang'])||p.version!==1||!text(p.title,1,160)||!Array.isArray(p.columns)||p.columns.length<2||p.columns.length>4||!p.columns.every(x=>text(x,1,80))||!Array.isArray(p.rows)||p.rows.length<1||p.rows.length>20)return null
  const width=p.columns.length
  if(!p.rows.every(row=>closed(row,['label','values'])&&text(row.label,1,120)&&Array.isArray(row.values)&&row.values.length===width&&row.values.every(v=>text(v,0,500))))return null
  if(p.openui_lang!==undefined&&!text(p.openui_lang,1,4096))return null
  return JSON.parse(JSON.stringify(value)) as NativeRecord
 } catch {return null}
}
export const recordKey=(r:NativeRecord)=>JSON.stringify({stored_session_id:r.identity.stored_session_id,assistant_row_id:r.identity.assistant_row_id,tool_call_id:r.identity.tool_call_id,turn_row_id:r.identity.turn_row_id})
