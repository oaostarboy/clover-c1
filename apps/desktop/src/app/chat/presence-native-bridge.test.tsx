import { existsSync, readFileSync, writeFileSync } from 'node:fs'

import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { $presenceFeed, acceptPresenceInitiative, presenceKey } from '@/store/presence'
import { $activeGatewayProfile } from '@/store/profile'

import { ComposerStatusStack } from './composer/status-stack'

// Invoked by the canonical Python adapter test while that exact owner is alive.
// The frame and RPC response come from the real server, not static fixtures.
const fixture = process.env.CLOVER_PRESENCE_NATIVE_FRAME

describe.skipIf(!fixture)('native presence server -> mounted renderer -> actual owner ACK', () => {
  it('renders the actual native frame without chat, focus or navigation and round-trips its ACK', async () => {
    vi.stubGlobal('fetch', () => { throw new Error('External network is disabled in this bridge') })
    const frame = JSON.parse(readFileSync(fixture!, 'utf8'))
    const { session_id: sid, payload } = frame.params
    const profile = $activeGatewayProfile.get()
    $presenceFeed.set({})
    render(<MemoryRouter><ComposerStatusStack queue={null} sessionId={sid} /></MemoryRouter>)

    const request = async (method: string, params: Record<string, unknown>) => {
      await waitFor(() => expect(screen.getByText(payload.text)).toBeTruthy())
      expect(globalThis.document.activeElement).toBe(globalThis.document.body)
      expect(screen.getAllByRole('button')).toHaveLength(4)
      writeFileSync(fixture!+'.rpc.json', JSON.stringify({ jsonrpc: '2.0', id: 'actual-renderer-ack', method, params }))
      await waitFor(() => expect(existsSync(fixture!+'.response.json')).toBe(true), { timeout: 3000 })

      return JSON.parse(readFileSync(fixture!+'.response.json', 'utf8')).result
    }

    let completion: Promise<boolean>
    act(() => { completion = acceptPresenceInitiative(profile, sid, payload, request) })
    const accepted = await completion!
    expect(accepted).toBe(true)
    expect($presenceFeed.get()[presenceKey(profile,sid)]?.text).toBe(payload.text)
    writeFileSync(fixture!+'.rendered.json', JSON.stringify({
      text: screen.getByRole('status').textContent,
      activeElement: globalThis.document.activeElement?.tagName,
      controls: screen.getAllByRole('button').map(button => button.textContent), accepted
    }))
  })
})
