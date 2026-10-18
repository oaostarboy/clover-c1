import { describe, expect, it, vi } from 'vitest'

import { $presenceFeed, acceptPresenceInitiative, presenceKey } from './presence'

const payload = { type: 'presence.initiative', schema_version: 1, thought_id: 'thought-1', revision: 1,
  event_id: 'event-1', epoch: 0, generation: 1, text: 'A thought to check: verify manifest checksum.',
  ack_token: 'synthetic-ack', control_token: 'synthetic-control', focus: false, navigate: false }

describe('presence status feed owner', () => {
  it('publishes a scoped row before acknowledging, without focus or navigation', async () => {
    $presenceFeed.set({})

    const request = vi.fn(async () => {
      expect($presenceFeed.get()[presenceKey('profile-a', 'session-a')]?.text).toBe(payload.text)

      return { accepted: true }
    })

    expect(await acceptPresenceInitiative('profile-a', 'session-a', payload, request)).toBe(true)
    expect(request).toHaveBeenCalledWith('presence.ack', { session_id: 'session-a', token: payload.ack_token })
    expect($presenceFeed.get()[presenceKey('profile-b', 'session-a')]).toBeUndefined()
    expect(await acceptPresenceInitiative('profile-a', 'session-a', payload, request)).toBe(false)
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('holds malformed or focus-stealing frames without ACK', async () => {
    $presenceFeed.set({})
    const request = vi.fn()
    expect(await acceptPresenceInitiative('profile-a', 'session-a', { ...payload, focus: true }, request)).toBe(false)
    expect(await acceptPresenceInitiative('profile-a', 'session-a', { ...payload, generation: NaN }, request)).toBe(false)
    expect(request).not.toHaveBeenCalled()
    expect($presenceFeed.get()).toEqual({})
  })
})
