import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { $subagentsBySession, upsertSubagent } from '@/store/subagents'

import { ActivityPresence } from './activity-presence'
import { DelegationBerths } from './delegation-particles'

vi.mock('./clover-particles', () => ({ CloverParticles: () => <canvas /> }))
afterEach(() => {
  cleanup()
  $subagentsBySession.set({})
  vi.unstubAllGlobals()
})
it('buds only new current-session helpers and docks without completing them', () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }))
  const turnStartedAt = Date.now() - 10

  const { container, rerender } = render(
    <>
      <ActivityPresence active phase="working" sessionId="particle-session" turnStartedAt={turnStartedAt} />
      <DelegationBerths sessionId="particle-session" />
    </>
  )

  act(() => upsertSubagent('other', { subagent_id: 'elsewhere', goal: 'Other' }))
  expect(container.querySelectorAll('[data-helper-berth]')).toHaveLength(0)
  act(() => upsertSubagent('particle-session', { subagent_id: 'one', goal: 'Real task', status: 'running' }))
  expect(container.querySelectorAll('[data-helper-berth]')).toHaveLength(1)
  rerender(
    <>
      <ActivityPresence active={false} phase="working" sessionId="particle-session" turnStartedAt={turnStartedAt} />
      <DelegationBerths sessionId="particle-session" />
    </>
  )
  expect($subagentsBySession.get()['particle-session'][0].status).toBe('running')
  act(() => upsertSubagent('particle-session', { subagent_id: 'one', status: 'failed' }, false, 'subagent.complete'))
  expect(container.querySelector('[data-helper-berth]')?.getAttribute('data-status')).toBe('failed')
})
