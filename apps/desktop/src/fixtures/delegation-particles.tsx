import '../../src/styles.css'
import '../../src/components/assistant-ui/thread/transcript.css'

import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
/// <reference types="vite/client" />
import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'

import { ChatBar } from '../../src/app/chat/composer'
import { handleToolEvent } from '../../src/app/session/hooks/use-message-stream/gateway-event/tools'
import type { CloverGateway } from '../../src/clover'
import { Thread } from '../../src/components/assistant-ui/thread'
import { Wordmark } from '../../src/components/chat/wordmark'
import { $gateway } from '../../src/store/gateway'
import { $activeSessionId, $busy, $turnStartedAt } from '../../src/store/session'
import { $subagentsBySession } from '../../src/store/subagents'
import { ThemeProvider, useTheme } from '../../src/themes/context'

const requests: { method: string; params: unknown }[] = []
$gateway.set({
  request: async (method: string, params: unknown) => {
    requests.push({ method, params })

    return method === 'subagent.interrupt' ? { found: true } : { processes: [] }
  }
} as unknown as CloverGateway)
const createdAt = new Date()

const user = (id: string, text: string): ThreadMessage =>
  ({
    id,
    role: 'user',
    content: [{ type: 'text', text }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  }) as ThreadMessage

const assistant = (id: string, text: string): ThreadMessage =>
  ({
    id,
    role: 'assistant',
    content: [{ type: 'text', text }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { custom: {}, unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [] }
  }) as ThreadMessage

const envelope =
  '[ASYNC DELEGATION BATCH COMPLETE — deleg_fixture]\nA background fan-out of 1 subagent(s) you dispatched earlier has finished.\nRole: default   Model: fixture   Total duration: 2s'

function Fixture() {
  const { setMode } = useTheme()
  const [sessionId, setSession] = useState('fixture-one')
  const [thinking, setThinking] = useState(true)

  const [messages, setMessages] = useState<ThreadMessage[]>([
    user('human', 'Please check the desktop changes.'),
    {
      ...assistant('answer', 'The existing composer stays unchanged.'),
      content: [
        {
          type: 'reasoning',
          text: 'Fixture provider summary: inspect the real components and retain readable details.'
        },
        {
          type: 'tool-call',
          toolCallId: 'fixture-delegation',
          toolName: 'delegate_task',
          args: { goal: 'Inspect production components' }
        },
        { type: 'text', text: 'The existing composer stays unchanged.' }
      ]
    } as ThreadMessage,
    user('internal', envelope),
    assistant('silent', '[SILENT]')
  ])

  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages,
    isRunning: false,
    onNew: async () => {},
    onEdit: async () => {},
    onCancel: async () => {}
  })

  React.useEffect(() => {
    $activeSessionId.set(sessionId)
    $turnStartedAt.set(Date.now())
    $busy.set(true)
    const nativeSubagentSessionsRef = { current: new Set<string>() }

    const event = (type: string, payload: object) =>
      handleToolEvent({
        event: { type },
        payload,
        sessionId,
        deps: { nativeSubagentSessionsRef, sessionInterrupted: () => false }
      } as any)

    ;(window as any).delegationFixture = {
      spawn: (id: string) =>
        event('subagent.start', {
          subagent_id: id,
          goal: `Inspect production helper ${id}`,
          model: 'fixture/provider-model',
          status: 'running',
          tool_count: 2
        }),
      thinking: (id: string, text: string) => event('subagent.thinking', { subagent_id: id, text }),
      terminal: (id: string, status: string) =>
        event('subagent.complete', { subagent_id: id, status, summary: `Backend ${status}` }),
      todos: () =>
        event('todo.updated', {
          revision: 1,
          todos: [
            { id: 'one', content: 'Inspect the desktop', status: 'completed' },
            { id: 'two', content: 'Verify the helpers', status: 'in_progress' }
          ]
        }),
      theme: (mode: 'dark' | 'light') => setMode(mode),
      finish: () => {
        $busy.set(false)
        setThinking(false)
      },
      switchSession: () => {
        setSession('fixture-two')
        setThinking(false)
      },
      messages: (text: string) =>
        setMessages([user('human', text), assistant('answer', 'The existing composer stays unchanged.')]),
      glassMessages: () => setMessages([
        user('glass-one', 'First short prompt'), assistant('glass-answer-one', 'First answer.'),
        user('glass-two', 'Second short prompt'), assistant('glass-answer-two', 'Second answer.'),
        user('glass-three', 'Third short prompt'),
        assistant('glass-answer-three', Array.from({ length: 90 }, () => 'UNDERLYING GLYPHS SHOULD NOT BLEED THROUGH THE GLASS BUBBLE '.repeat(5)).join('\n\n'))
      ]),
      requests: () => requests,
      state: () => $subagentsBySession.get()[sessionId]
    }
  }, [sessionId, setMode])

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div
        data-editorial-thread=""
        style={{
          width: 'min(780px, calc(100% - 48px))',
          margin: '0 auto',
          height: '100vh',
          display: 'flex',
          flexDirection: 'column'
        }}
      >
        <Wordmark text="Fixture Bot" />
        <div style={{ fontSize: 11, padding: 12, color: 'var(--ui-text-tertiary)' }}>
          Production component fixture · injected test events, not live worker output
        </div>
        <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
          <Thread sessionId={sessionId} />
        </div>

        <ChatBar
          busy={thinking}
          disabled={false}
          onCancel={() => setThinking(false)}
          onSubmit={() => true}
          sessionId={sessionId}
          state={{
            model: { model: 'fixture/provider-model', provider: 'fixture', canSwitch: false },
            tools: { enabled: true, label: 'Tools' },
            voice: { enabled: true, active: false }
          }}
        />
      </div>
    </AssistantRuntimeProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <ThemeProvider>
      <Fixture />
    </ThemeProvider>
  </MemoryRouter>
)
