import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { $presenceFeed, presenceKey, registerPresenceReturn } from '@/store/presence'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId } from '@/store/session'

import { ComposerStatusStack } from './composer/status-stack'
import { PresenceCard } from './presence-card'

vi.mock('@/store/notifications', () => ({ notify: vi.fn() }))

describe('presence status card', () => {
  it('produces a project return only for the selected, backend-enabled conversation', async () => {
    const profile = $activeGatewayProfile.get()
    const request = vi.fn(async () => ({ accepted: true }))
    $activeSessionId.set('other')
    registerPresenceReturn(profile, 'return-session', request)
    render(<MemoryRouter><ComposerStatusStack queue={null} sessionId="return-session" /></MemoryRouter>)
    expect(request).not.toHaveBeenCalled()
    act(() => $activeSessionId.set('return-session'))
    await waitFor(() => expect(request).toHaveBeenCalledWith('presence.project_return', { session_id: 'return-session' }))
    expect(request).toHaveBeenCalledTimes(1)
    registerPresenceReturn(profile, 'return-session', null)
    act(() => $activeSessionId.set('other'))
    act(() => $activeSessionId.set('return-session'))
    expect(request).toHaveBeenCalledTimes(1)
  })
  it('mounts in the actual composer status feed without creating a chat message', () => {
    const profile = $activeGatewayProfile.get()
    $presenceFeed.set({ [presenceKey(profile, 'scope-session')]: {
      profile, sessionId: 'scope-session', text: 'Check this source-backed connection.', thoughtId: 't',
      revision: 1, epoch: 0, generation: 1, eventId: 'e', controlToken: 'opaque', request: vi.fn()
    } })
    render(<MemoryRouter><ComposerStatusStack queue={null} sessionId="scope-session" /></MemoryRouter>)
    expect(screen.getByText('Check this source-backed connection.')).toBeTruthy()
  })
  it('renders bounded text and sends an explicit scoped Stop without navigation', async () => {
    const request = vi.fn(async () => ({ accepted: true, text: 'Presence stopped.' }))
    render(<PresenceCard entry={{ profile: 'p', sessionId: 's', text: 'Check the manifest checksum.',
      thoughtId: 't', revision: 1, epoch: 0, generation: 1, eventId: 'e', controlToken: 'opaque', request }} />)
    expect(screen.getByRole('status').textContent).toContain('Check the manifest checksum.')
    expect(globalThis.document.activeElement).toBe(globalThis.document.body)
    fireEvent.click(screen.getByRole('button', { name: 'Stop everywhere' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith('presence.control', { session_id: 's', token: 'opaque', action: 'stop' }))
  })
})
