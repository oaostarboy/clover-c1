import {Component,lazy,type ReactNode,Suspense,useContext,useEffect,useState} from 'react'

import {type NativeRecord,recordKey,validateRecord} from '../../../../../../integrations/generative-ui/contract'

import {ThreadBackendContext} from './thread-backend-context'
const Comparison=lazy(()=>import('./registered-comparison'))
class OptionalBoundary extends Component<{children:ReactNode},{failed:boolean}> {
 state={failed:false}
 static getDerivedStateFromError(){return {failed:true}}
 render(){return this.state.failed?<p role="status">Comparison view unavailable. The normal tool result remains below.</p>:this.props.children}
}
/** No generic args/result trust path. Old/disabled backends leave this empty. */
export function OptionalComparison({toolCallId}:{toolCallId:string}) {
 const {gateway,sessionId}=useContext(ThreadBackendContext)
 const [record,setRecord]=useState<NativeRecord|null>(null)
 const [offline,setOffline]=useState(false)
 useEffect(()=>{
  let active=true;setRecord(null);setOffline(false)
  if(!gateway||!sessionId){return}
  const accept=(raw:unknown)=>{
   const incoming=validateRecord(raw)
   if(!active||!incoming||incoming.identity.tool_call_id!==toolCallId){return}
   setRecord(previous=>{
    if(previous&&(recordKey(previous)!==recordKey(incoming)||previous.status!=='running'||incoming.revision<=previous.revision)){return previous}
    return incoming
   })
  }
  const off=gateway.onEvent(event=>{
   if(event.session_id===sessionId&&event.type==='tool.semantic'){accept(event.payload)}
  })
  const hydrate=()=>void gateway.request<{structured_tools?:unknown[]}>('session.history',{session_id:sessionId}).then(state=>{
   if(!active){return}
   setOffline(false)
   const records=Array.isArray(state.structured_tools)&&state.structured_tools.length<=128?state.structured_tools.map(validateRecord).filter(r=>r?.identity.tool_call_id===toolCallId):[]
   if(records.length===1){accept(records[0])}
  }).catch(()=>{if(active){setOffline(true)}})
  const offState=gateway.onState?.(state=>{if(!active){return;}if(state==='open'){hydrate();}else {setOffline(true)}})
  hydrate()
  return ()=>{active=false;off();offState?.()}
 },[gateway,sessionId,toolCallId])
 if(!record){return null}
 return <OptionalBoundary key={recordKey(record)}><Suspense fallback={<p role="status">Opening comparison view…</p>}><Comparison offline={offline} record={record}/></Suspense></OptionalBoundary>
}
