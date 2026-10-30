import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const stores = vi.hoisted(() => ({ owner: null as any, connection: null as any }))
vi.mock('@clover/plugin-sdk', async () => {
  const { atom } = await import('nanostores')
  stores.owner = atom(null)
  stores.connection = atom('local')

  return {
    atom,
    host: { state: { connectionId: stores.connection, focusedSessionOwner: stores.owner, profile: atom('default') } },
    queryClient: {}, useQuery: vi.fn(), useValue: vi.fn()
  }
})
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { composer: {
  newSessionPlaceholders: ['Give Clover a task'], followUpPlaceholders: ['Send a follow-up'],
  placeholderStarting: 'Starting Clover...', placeholderReconnecting: 'Reconnecting to Clover…'
} } }) }))

import { $botMeta, $lastRoster } from '@/plugins/clover-bots/data'
import { _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { useComposerPlaceholder } from './use-composer-placeholder'

function Input({ landing = false, disabled = false, reconnecting = false, sessionId = null }: { landing?: boolean; disabled?: boolean; reconnecting?: boolean; sessionId?: null | string }) {
  const placeholder = useComposerPlaceholder({ disabled, landing, reconnecting, sessionId })

  return <div contentEditable data-placeholder={placeholder} role="textbox" />
}

beforeEach(() => {
  _resetSessionOwnerHintsForTests()
  $sessionTiles.set([])
  stores.connection.set('local')
  stores.owner.set({ connectionId: 'source-a', profile: 'default' })
  $botMeta.set({})
  $lastRoster.set([
    { name: 'default', connectionId: 'source-a', remoteSource: true, display_name: 'Zyra' },
    { name: 'default', connectionId: 'source-b', remoteSource: true, display_name: 'Oracle' },
    { name: 'worker', connectionId: 'source-a', remoteSource: true, display_name: 'Suni' }
  ])
})

afterEach(cleanup)

describe('composer identity follows the focused chat owner', () => {
  it('reacts when an owner hint arrives late and becomes ambiguous', () => {
    render(<Input landing sessionId="late" />)
    const input = screen.getByRole('textbox')
    expect(input.getAttribute('data-placeholder')).toBe('Ask Clover anything…')
    act(() => setSessionOwnerHint('late', { connectionId: 'source-b', profile: 'default' }))
    expect(input.getAttribute('data-placeholder')).toBe('Ask Oracle anything…')
    act(() => setSessionOwnerHint('late', { connectionId: 'source-a', profile: 'default' }))
    expect(input.getAttribute('data-placeholder')).toBe('Ask Clover anything…')
  })

  it('keeps each session scoped to its own owner as focus and routes change', () => {
    $sessionTiles.set([
      { storedSessionId: 'a', ownerRoute: { connectionId: 'source-a', profile: 'default' } },
      { storedSessionId: 'b', ownerRoute: { connectionId: 'source-b', profile: 'default' } }
    ])
    render(<><Input landing sessionId="a" /><Input landing sessionId="b" /><Input landing /><Input landing sessionId="unknown" /></>)
    const inputs = screen.getAllByRole('textbox')
    const names = () => inputs.map(input => input.getAttribute('data-placeholder'))
    expect(names()).toEqual(['Ask Zyra anything…', 'Ask Oracle anything…', 'Ask Zyra anything…', 'Ask Clover anything…'])
    act(() => stores.owner.set({ connectionId: 'source-b', profile: 'default' }))
    expect(names()).toEqual(['Ask Zyra anything…', 'Ask Oracle anything…', 'Ask Oracle anything…', 'Ask Clover anything…'])
    act(() => $sessionTiles.set([
      { storedSessionId: 'a', ownerRoute: { connectionId: 'source-a', profile: 'worker' } },
      { storedSessionId: 'b', ownerRoute: { connectionId: 'source-b', profile: 'default' } },
      { storedSessionId: 'unknown', ownerRoute: { connectionId: 'source-a', profile: 'default' } }
    ]))
    expect(names()).toEqual(['Ask Suni anything…', 'Ask Oracle anything…', 'Ask Oracle anything…', 'Ask Zyra anything…'])
  })
  it('updates the actual input for profile and connection switches without borrowing another owner', () => {
    const { rerender } = render(<Input landing />)
    const input = screen.getByRole('textbox')
    expect(input.getAttribute('data-placeholder')).toBe('Ask Zyra anything…')
    act(() => stores.owner.set({ connectionId: 'source-a', profile: 'worker' }))
    expect(input.getAttribute('data-placeholder')).toBe('Ask Suni anything…')
    act(() => stores.owner.set({ connectionId: 'source-b', profile: 'default' }))
    expect(input.getAttribute('data-placeholder')).toBe('Ask Oracle anything…')
    act(() => stores.owner.set({ connectionId: 'unknown', profile: 'default' }))
    expect(input.getAttribute('data-placeholder')).toBe('Ask Clover anything…')
    act(() => stores.owner.set({ connectionId: 'source-a', profile: 'default' }))
    act(() => $botMeta.set({ 'source-a::default': { title: 'Captain' } }))
    expect(input.getAttribute('data-placeholder')).toBe('Ask Captain anything…')
    rerender(<Input />)
    expect(input.getAttribute('data-placeholder')).toBe('Give Captain a task')
    rerender(<Input disabled landing reconnecting />)
    expect(input.getAttribute('data-placeholder')).toBe('Reconnecting to Clover…')
    rerender(<Input disabled landing />)
    expect(input.getAttribute('data-placeholder')).toBe('Starting Clover...')
  })

  it('does not use an unscoped local row for a remote owner or stale selection', () => {
    $lastRoster.set([{ name: 'default', display_name: 'Local' }])
    render(<Input landing />)
    expect(screen.getByRole('textbox').getAttribute('data-placeholder')).toBe('Ask Clover anything…')
    act(() => stores.owner.set(null))
    expect(screen.getByRole('textbox').getAttribute('data-placeholder')).toBe('Ask Clover anything…')
  })

  it('falls back to Clover for an unnamed local default', () => {
    stores.owner.set({ connectionId: 'local', profile: 'default' })
    $lastRoster.set([{ name: 'default' }])
    render(<Input landing />)
    expect(screen.getByRole('textbox').getAttribute('data-placeholder')).toBe('Ask Clover anything…')
  })
})
