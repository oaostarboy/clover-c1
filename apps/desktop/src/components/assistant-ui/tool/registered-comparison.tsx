import {useContext,useEffect,useMemo,useState} from 'react'
import {CloverActivityAgent,validateRecord,recordKey,type NativeRecord} from '../../../../../../integrations/generative-ui/adapter'
import {Button} from '@/components/ui/button'
import {notifyError} from '@/store/notifications'
import {ThreadBackendContext} from './thread-backend-context'
export default function RegisteredComparison({record,offline}:{record:NativeRecord;offline:boolean}) {
 const {gateway,sessionId}=useContext(ThreadBackendContext)
 const key=recordKey(record)
 const agent=useMemo(()=>new CloverActivityAgent(record,()=>gateway?.request('session.interrupt',{session_id:sessionId}).catch(error=>notifyError(error,'Stop failed'))),[key,gateway,sessionId])
 const [shown,setShown]=useState<NativeRecord|null>(null)
 const [view,setView]=useState<'table'|'list'|'dsl'>('table')
 useEffect(()=>{
  let mounted=true
  void agent.connectAgent({runId:agent.nativeRunId},{onMessagesChanged:({messages})=>{
   if(!mounted)return
   const current=messages.map(m=>m.role==='activity'?validateRecord(m.content):null).find(r=>r&&recordKey(r)===key)
   if(current)setShown(current)
  }}).catch(()=>{if(mounted)setShown(null)})
  return ()=>{mounted=false;agent.closeView()}
 },[agent,key])
 useEffect(()=>{agent.accept(record)},[agent,record])
 if(!shown)return <p role="status">Opening comparison view…</p>
 const p=shown.payload
 const status={running:'Waiting for the tool result',success:'Complete',error:'The tool reported an error',cancelled:'Stopped',obsolete:'Replaced by a newer turn'}[shown.status]
 const hasOpenUI = !!p.openui_lang || shown.component === 'clover.openui.comparison'
 return <section aria-label={p.title} data-registered-comparison="" data-openui-renderer="" className="my-3 min-w-0 text-(--ui-text-primary)">
  <div className="flex flex-wrap items-center justify-between gap-2">
   <div className="flex items-center gap-2">
    {hasOpenUI && (
     <span className="inline-flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 text-[0.6875rem] font-medium tracking-wide text-emerald-400 border border-emerald-500/20" aria-label="Component source">OpenUI</span>
    )}
    <h3 className="text-[0.9375rem] font-semibold tracking-tight text-(--ui-text-primary)">{p.title}</h3>
   </div>
   <div className="flex gap-1" aria-label="Comparison view">
    <Button variant="ghost" size="touch" aria-label="Table view" aria-pressed={view==='table'} onClick={()=>setView('table')}>Table</Button>
    <Button variant="ghost" size="touch" aria-label="List view" aria-pressed={view==='list'} onClick={()=>setView('list')}>List</Button>
    {p.openui_lang ? (
     <Button variant="ghost" size="touch" aria-label="OpenUI Lang source view" aria-pressed={view==='dsl'} onClick={()=>setView('dsl')}>Lang</Button>
    ) : null}
   </div>
  </div>
  <p role="status" className="mb-2 text-xs text-(--ui-text-secondary)">{offline?'Offline. Showing the last saved state. ':''}{status}</p>
  {view==='table' ? (
   <div className="min-w-0 overflow-x-auto">
    <table className="w-full table-fixed text-left text-[0.8125rem] leading-normal">
     <thead><tr><th scope="col" className="py-2 pr-2 text-xs font-semibold uppercase tracking-wider text-(--ui-text-secondary)">Item</th>{p.columns.map((column,i)=><th scope="col" key={i} className="break-words px-2 py-2 text-xs font-semibold uppercase tracking-wider text-(--ui-text-secondary)">{column}</th>)}</tr></thead>
     <tbody>{p.rows.map((row,i)=><tr key={i} className="border-t border-(--ui-stroke-tertiary)"><th scope="row" className="break-words py-2 pr-2 font-medium text-(--ui-text-primary)">{row.label}</th>{row.values.map((value,j)=><td key={j} className="px-2 py-2 text-(--ui-text-secondary) [overflow-wrap:anywhere]">{value}</td>)}</tr>)}</tbody>
    </table>
   </div>
  ) : view==='list' ? (
   <dl className="grid gap-3 text-sm">{p.rows.map((row,i)=><div key={i}><dt className="font-medium">{row.label}</dt>{row.values.map((value,j)=><dd key={j} className="[overflow-wrap:anywhere]">{p.columns[j]}: {value}</dd>)}</div>)}</dl>
  ) : (
   <div className="rounded border border-(--ui-stroke-tertiary) bg-slate-950 p-3 font-mono text-xs text-emerald-400 overflow-x-auto">
    <pre><code>{p.openui_lang}</code></pre>
   </div>
  )}
 </section>
}
