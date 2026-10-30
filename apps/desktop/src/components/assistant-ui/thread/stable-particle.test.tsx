import { type ThreadMessage } from '@assistant-ui/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, expect, it, vi } from 'vitest'

import { PRIMARY_SESSION_VIEW, SessionViewProvider } from '@/app/chat/session-view'
import { $compactingSessions, setSessionCompacting } from '@/store/compaction'
import { clearApprovalRequest, setApprovalRequest } from '@/store/prompts'

import { stubThreadEnvironment, ThreadRuntime, userMessage } from '../test-utils'

import { setReasoningCollapsedByDefault } from '@/store/reasoning-disclosure'
import { $delegationAnchors } from './delegation-anchor'

import { Thread } from '.'

stubThreadEnvironment()

function assistant(id: string, content: unknown[], running = true): ThreadMessage {
  return {
    id, role: 'assistant', content, status: running ? { type: 'running' } : { type: 'complete', reason: 'stop' },
    createdAt: new Date(), metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as unknown as ThreadMessage
}

it('keeps one formation and anchor across reasoning, tools and interim messages until the session settles', async () => {
  setReasoningCollapsedByDefault(false) // This test observes phase content, not the default disclosure policy.
  const busy = atom(true)
  const view = { ...PRIMARY_SESSION_VIEW, $runtimeId: atom('stable-session'), $busy: busy, $turnStartedAt: atom<number | null>(Date.now()) }
  const user = userMessage('u', 'work')
  const thought = { type: 'reasoning', text: 'thinking' }
  const tool = { type: 'tool-call', toolName: 'terminal', toolCallId: 't', args: { command: 'pwd' }, argsText: '{"command":"pwd"}' }
  const tree = (messages: ThreadMessage[]) => <SessionViewProvider value={view}><ThreadRuntime messages={messages}><Thread /></ThreadRuntime></SessionViewProvider>
  const { container, rerender } = render(tree([user, assistant('a', [])]))
  const anchor = container.querySelector('.clover-activity-anchor')
  expect(anchor).not.toBeNull()
  const canvas = anchor?.querySelector('canvas')
  expect(canvas).not.toBeNull()

  const check = () => {
    expect(container.querySelectorAll('.clover-activity-anchor')).toHaveLength(1)
    expect(container.querySelectorAll('canvas.clover-formation')).toHaveLength(1)
    expect(container.querySelector('.clover-activity-anchor')).toBe(anchor)
    expect(anchor?.querySelector('canvas')).toBe(canvas)
    expect($delegationAnchors.get()['stable-session']?.element).toBe(anchor)
  }

  check()
  rerender(tree([user, assistant('a', [thought])]))
  await waitFor(() => expect(container.textContent).toContain('thinking'))
  check()
  act(() => setSessionCompacting('stable-session', true))
  expect(canvas?.getAttribute('data-clover-phase')).toBe('compacting')
  act(() => setSessionCompacting('stable-session', false))
  rerender(tree([user, assistant('a', [thought, tool])]))
  await waitFor(() => expect(canvas?.getAttribute('data-clover-phase')).toBe('tool'))
  check()
  expect(canvas?.getAttribute('data-clover-phase')).toBe('tool')
  rerender(tree([user, assistant('a', [thought, { ...tool, result: 'ok' }, { type: 'reasoning', text: 'more' }])]))
  await waitFor(() => expect(canvas?.getAttribute('data-clover-phase')).toBe('working'))
  check()
  rerender(tree([user, assistant('a', [thought], false), assistant('b', [{ type: 'reasoning', text: 'next' }])]))
  await waitFor(() => expect(container.textContent).toContain('next'))
  check()
  rerender(tree([user, assistant('b', [{ type: 'tool-call', toolName: 'delegate_task', toolCallId: 'delegate', args: { goal: 'Review code' } }])]))
  await waitFor(check)
  act(() => setApprovalRequest({ sessionId: 'stable-session', command: 'pwd', description: 'approve' }))
  expect(anchor?.querySelector('.clover-activity-art')?.getAttribute('data-state')).toBe('leaving')
  act(() => clearApprovalRequest('stable-session'))
  check()
  rerender(tree([user, assistant('a', [thought], false), assistant('b', [{ type: 'text', text: 'done' }], false)]))
  await waitFor(() => expect(container.textContent).toContain('done'))
  act(() => busy.set(false))
  expect(anchor?.querySelector('.clover-activity-art')?.getAttribute('data-state')).toBe('leaving')
  await waitFor(() => expect(container.querySelector('.clover-activity-anchor')).toBeNull())
})

afterEach(() => { setReasoningCollapsedByDefault(true); cleanup(); clearApprovalRequest(); $compactingSessions.set({}); vi.useRealTimers() })
