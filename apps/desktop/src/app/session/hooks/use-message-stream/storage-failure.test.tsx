import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import { renderMessageStream } from './test-harness'

const SID = 'storage-failure-session'
const TEXT = 'Real action receipt. Session save failed; reconcile before retrying.'

describe('storage failure after Stop', () => {
  afterEach(cleanup)

  it.each([
    { interrupted: false, code: 'session_persistence_failed:locked' },
    { interrupted: true, code: 'session_persistence_failed:locked' },
    { interrupted: false, code: 'session_persistence_recovered' },
    { interrupted: true, code: 'session_persistence_recovered' }
  ])('keeps real output and its nonretryable error when interrupted=$interrupted ($code)', ({ interrupted, code }) => {
    const state = createClientSessionState()
    state.interrupted = interrupted
    state.messages = [
      {
        id: 'existing-receipt',
        role: 'assistant',
        timestamp: 1,
        parts: [{ type: 'text', text: 'Real action receipt.' }]
      }
    ]
    const stream = renderMessageStream(SID, { states: new Map([[SID, state]]) })
    act(() =>
      stream.handleEvent({
        session_id: SID,
        type: 'message.complete',
        payload: {
          text: TEXT,
          status: 'error',
          partial: true,
          error: 'Session save failed; reconcile before retrying.',
          error_surface: { layer: 'disk', code, retryable: false }
        }
      })
    )
    const message = stream.state().messages.at(-1)
    expect(message?.parts.some(part => part.type === 'text' && part.text.includes('reconcile before retrying'))).toBe(
      true
    )
    expect(message?.errorSurface).toEqual({
      layer: 'disk',
      code,
      retryable: false
    })
    expect(stream.state().busy).toBe(false)

    if (interrupted) {
      expect(stream.state().interrupted).toBe(true)
    }
  })
})
