import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs'

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
    const sid = process.env.CLOVER_PRESENCE_NATIVE_SESSION!

    const writeAtomic = (suffix: string, value: unknown) => {
      const path = fixture! + suffix

      writeFileSync(path + '.tmp', JSON.stringify(value))
      renameSync(path + '.tmp', path)
    }

    const profile = $activeGatewayProfile.get()
    $presenceFeed.set({})
    render(<MemoryRouter><ComposerStatusStack queue={null} sessionId={sid} /></MemoryRouter>)
    expect(globalThis.document.activeElement).toBe(globalThis.document.body)
    writeAtomic('.ready.json', { session_id: sid, activeElement: globalThis.document.activeElement?.tagName, node: process.version })
    // Only after this mounted readiness signal does Python let the real owner
    // issue its five-second ticket. Startup/import time is not delivery time.
    await waitFor(() => expect(existsSync(fixture!)).toBe(true), { timeout: 30000 })
    const frame = JSON.parse(readFileSync(fixture!, 'utf8'))
    expect(frame.params.session_id).toBe(sid)
    const { payload } = frame.params

    const request = async (method: string, params: Record<string, unknown>) => {
      await waitFor(() => expect(screen.getByText(payload.text)).toBeTruthy())
      expect(globalThis.document.activeElement).toBe(globalThis.document.body)
      expect(screen.getAllByRole('button')).toHaveLength(4)
      writeAtomic('.rpc.json', { jsonrpc: '2.0', id: 'actual-renderer-ack', method, params })
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
  }, 45_000)
})
