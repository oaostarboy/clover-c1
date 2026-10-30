import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SubagentStrip } from './subagent-strip'
import { $subagentsBySession, upsertSubagent } from '@/store/subagents'
vi.mock('@/components/assistant-ui/thread/delegation-particles', () => ({ DelegationBerths: () => <span /> }))
const request = vi.fn().mockResolvedValue({ found: true })
vi.mock('@/store/session-states', () => ({ requestForOwnedSession: (...args: unknown[]) => request(...args) }))
vi.mock('@/store/windows', () => ({ openSessionInNewWindow: vi.fn() }))
afterEach(() => { cleanup(); $subagentsBySession.set({}); request.mockClear() })
it('keeps real thinking and Stop available without animation finishing the job', async () => {
  upsertSubagent('s', { subagent_id: 'a', goal: 'Check the real source', model: 'provider/model', status: 'running', tool_count: 4 })
  upsertSubagent('s', { subagent_id: 'a', text: 'Provider supplied summary' }, false, 'subagent.thinking')
  render(<SubagentStrip sessionId="s" />)
  fireEvent.click(screen.getByRole('button', { expanded: false }))
  expect(screen.getByText('provider/model')).toBeTruthy()
  expect(screen.getByText('Provider supplied summary')).toBeTruthy()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Stop' })))
  expect(request).toHaveBeenCalledWith('s', expect.any(Function), 'subagent.interrupt', { subagent_id: 'a' })
  expect($subagentsBySession.get().s[0].status).toBe('running')
  act(() => upsertSubagent('s', { subagent_id: 'a', status: 'failed', summary: 'Backend failure' }, false, 'subagent.complete'))
  expect(screen.getByText('Backend failure')).toBeTruthy()
})
