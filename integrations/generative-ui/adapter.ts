import { AbstractAgent } from '@ag-ui/client'
import { EventType, type BaseEvent, type RunAgentInput, type Message } from '@ag-ui/core'
import { Observable, Subject, concat, of, throwError } from 'rxjs'
import { validateRecord, recordKey, type NativeRecord } from './contract'
export { validateRecord, recordKey, type NativeRecord } from './contract'

/** Read-only official AG-UI connection over Clover's existing canonical stream.
 * Connecting attaches a view to the persisted turn; it never invokes a model.
 * Only existing native Stop owns interruption. Detaching does not cancel work.
 */
export class CloverActivityAgent extends AbstractAgent {
 readonly nativeRunId: string
 private changes = new Subject<BaseEvent>()
 private records = new Map<string,NativeRecord>()
 private interrupt: () => unknown
 constructor(initial:NativeRecord,interrupt:()=>unknown) {
  const record=validateRecord(initial)
  if(!record)throw new Error('Unsupported structured component')
  super({agentId:'clover-native-activity',threadId:record.identity.stored_session_id})
  this.interrupt=interrupt
  this.nativeRunId=`${record.identity.stored_session_id}:${record.identity.turn_row_id}`
  this.records.set(recordKey(record),record)
 }
 run(_input:RunAgentInput):Observable<BaseEvent> {return throwError(()=>new Error('Read-only connection; submit through Clover'))}
 protected connect(input:RunAgentInput):Observable<BaseEvent> {
  return concat(of(
   {type:EventType.RUN_STARTED,threadId:this.threadId,runId:this.nativeRunId} as BaseEvent,
   {type:EventType.MESSAGES_SNAPSHOT,messages:this.activityMessages()} as BaseEvent
  ),this.changes)
 }
 activityMessages():Message[] {
  return [...this.records.values()].map(r=>({id:recordKey(r),role:'activity',activityType:r.component,content:r})) as Message[]
 }
 accept(raw:unknown):boolean {
  const incoming=validateRecord(raw)
  if(!incoming||incoming.identity.stored_session_id!==this.threadId)return false
  const key=recordKey(incoming),previous=this.records.get(key)
  if(previous&&(previous.status!=='running'||incoming.revision<=previous.revision))return false
  if(!previous&&this.records.size>=128)return false
  this.records.set(key,incoming)
  this.changes.next({type:EventType.ACTIVITY_SNAPSHOT,messageId:key,activityType:incoming.component,content:incoming,replace:true} as BaseEvent)
  return true
 }
 abortRun():void {
  void this.interrupt()
  this.changes.complete()
  super.abortRun()
 }
 closeView():void {this.changes.complete();void this.detachActiveRun()}
}
