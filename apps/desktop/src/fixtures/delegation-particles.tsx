/// <reference types="vite/client" />
import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { DelegateTool } from '../../src/components/assistant-ui/tool/delegate'
import { Wordmark } from '../../src/components/chat/wordmark'
import { $gateway } from '../../src/store/gateway'
import type { CloverGateway } from '../../src/clover'
import { Thread } from '../../src/components/assistant-ui/thread'
import { ChatBar } from '../../src/app/chat/composer'
import { $activeSessionId, $turnStartedAt, $busy } from '../../src/store/session'
import { $subagentsBySession, upsertSubagent } from '../../src/store/subagents'
import '../../src/styles.css'
import '../../src/components/assistant-ui/thread/transcript.css'

const requests: { method: string; params: unknown }[] = []
$gateway.set({ request: async (method: string, params: unknown) => { requests.push({ method, params }); return method === 'subagent.interrupt' ? { found: true } : { processes: [] } } } as unknown as CloverGateway)
const createdAt = new Date()
const user = (id: string, text: string): ThreadMessage => ({ id, role: 'user', content: [{ type: 'text', text }], attachments: [], createdAt, metadata: { custom: {} } }) as ThreadMessage
const assistant = (id: string, text: string): ThreadMessage => ({ id, role: 'assistant', content: [{ type: 'text', text }], status: { type: 'complete', reason: 'stop' }, createdAt, metadata: { custom: {}, unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [] } }) as ThreadMessage
const envelope = '[ASYNC DELEGATION BATCH COMPLETE — deleg_fixture]\nA background fan-out of 1 subagent(s) you dispatched earlier has finished.\nRole: default   Model: fixture   Total duration: 2s'

function Fixture() {
  const [sessionId, setSession] = useState('fixture-one')
  const [thinking, setThinking] = useState(true)
  const [messages, setMessages] = useState<ThreadMessage[]>([
    user('human', 'Please check the desktop changes.'),
    { ...assistant('answer', 'The existing composer stays unchanged.'), content: [{ type: 'reasoning', text: 'Fixture provider summary: inspect the real components and retain readable details.' }, { type: 'text', text: 'The existing composer stays unchanged.' }] } as ThreadMessage,
    user('internal', envelope), assistant('silent', '[SILENT]')
  ])
  const runtime = useExternalStoreRuntime<ThreadMessage>({ messages, isRunning: false, onNew: async () => {}, onEdit: async () => {}, onCancel: async () => {} })
  React.useEffect(() => {
    $activeSessionId.set(sessionId)
    $turnStartedAt.set(Date.now())
    $busy.set(true)
    ;(window as any).delegationFixture = {
      spawn: (id: string) => upsertSubagent(sessionId, { subagent_id: id, goal: `Inspect production helper ${id}`, model: 'fixture/provider-model', status: 'running', tool_count: 2 }),
      thinking: (id: string, text: string) => upsertSubagent(sessionId, { subagent_id: id, text }, false, 'subagent.thinking'),
      terminal: (id: string, status: string) => upsertSubagent(sessionId, { subagent_id: id, status, summary: `Backend ${status}` }, false, 'subagent.complete'),
      finish: () => { $busy.set(false); setThinking(false) },
      switchSession: () => { setSession('fixture-two'); setThinking(false) },
      messages: (text: string) => setMessages([user('human', text), assistant('answer', 'The existing composer stays unchanged.')]),
      requests: () => requests,
      state: () => $subagentsBySession.get()[sessionId]
    }
  }, [sessionId])
  return <AssistantRuntimeProvider runtime={runtime}>
    <div data-editorial-thread="" style={{ width: 'min(780px, calc(100% - 48px))', margin: '0 auto', height: '100vh', display: 'flex', flexDirection: 'column' }}>
      <Wordmark text="Fixture Bot" />
      <div style={{ fontSize: 11, padding: 12, color: 'var(--ui-text-tertiary)' }}>Production component fixture · injected test events, not live worker output</div>
      <div style={{ flex: 1, minHeight: 0, position: 'relative' }}><Thread sessionId={sessionId} /></div>
      <DelegateTool args={{ goal: 'Inspect production components' }} toolCallId="fixture-delegation" />
      <ChatBar busy={thinking} disabled={false} sessionId={sessionId} onCancel={() => setThinking(false)} onSubmit={() => true} state={{ model: { model: 'fixture/provider-model', provider: 'fixture', canSwitch: false }, tools: { enabled: true, label: 'Tools' }, voice: { enabled: true, active: false } }} />
    </div>
  </AssistantRuntimeProvider>
}
createRoot(document.getElementById('root')!).render(<MemoryRouter><Fixture /></MemoryRouter>)
