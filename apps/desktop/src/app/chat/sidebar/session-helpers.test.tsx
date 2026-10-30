import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { listSessionHelpers } from '@/api/sessions'
import type { SessionInfo } from '@/clover'
import { makeSessionInfo } from '@/test/session-info'

import { SidebarHelpers } from './session-helpers'

vi.mock('@/api/sessions', () => ({ listSessionHelpers: vi.fn() }))
vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: { sidebar: { loadMore: 'Load more', row: { backgroundRunning: 'Background task running' } } } })
}))
vi.mock('./session-row', () => ({
  SidebarSessionRow: ({
    session,
    onResume,
    isSelected
  }: {
    session: SessionInfo
    onResume: () => void
    isSelected: boolean
  }) => (
    <button aria-pressed={isSelected} onClick={onResume} type="button">
      {session.id}
    </button>
  )
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const actions = () => ({
  activeSessionId: 'worker-a',
  onArchiveSession: vi.fn(),
  onDeleteSession: vi.fn(),
  onResumeSession: vi.fn(),
  onTogglePin: vi.fn(),
  onToggleUnread: vi.fn()
})

const worker = (id: string, is_active = false) => makeSessionInfo({ id, is_active, source: 'cli' })

describe('explicit helper subgroups', () => {
  it('keeps simultaneous owners separate and opens the full worker with the parent profile/connection', () => {
    const a = makeSessionInfo({
      id: 'a-tip',
      _lineage_root_id: 'a-root',
      profile: 'profile-a',
      connection_id: 'remote-a',
      helpers: [worker('worker-a', true)]
    })

    const b = makeSessionInfo({ id: 'b', helpers: [worker('worker-b')] })
    const handlers = actions()

    const { container } = render(
      <>
        <SidebarHelpers parent={a} {...handlers} />
        <SidebarHelpers parent={b} {...actions()} />
      </>
    )

    const groupA = container.querySelector('[data-session-helpers="a-tip"]') as HTMLElement
    const groupB = container.querySelector('[data-session-helpers="b"]') as HTMLElement
    expect(within(groupA).getByText('Helpers (1)')).toBeTruthy()
    expect(within(groupA).queryByText('worker-b')).toBeNull()
    expect(within(groupB).queryByText('worker-a')).toBeNull()
    expect(within(groupA).getByRole('status').textContent).toBe('Background task running')
    fireEvent.click(within(groupA).getByText('worker-a'))
    expect(handlers.onResumeSession).toHaveBeenCalledWith(
      'worker-a',
      expect.objectContaining({ profile: 'profile-a', connection_id: 'remote-a' })
    )
    expect(within(groupA).getByText('worker-a').getAttribute('aria-pressed')).toBe('true')
  })

  it('does not invent a subgroup for unlinked historical workers or ordinary branches', () => {
    const { container } = render(
      <SidebarHelpers
        parent={makeSessionInfo({ id: 'unlinked', parent_session_id: 'ordinary-branch' })}
        {...actions()}
      />
    )

    expect(container.childElementCount).toBe(0)
  })

  it('loads bounded extra pages without turning helpers into roots or losing preview refreshes', async () => {
    vi.mocked(listSessionHelpers).mockResolvedValue({
      sessions: [worker('worker-b')],
      has_more: false,
      limit: 20,
      offset: 1
    })
    const parent = makeSessionInfo({ id: 'parent', helpers: [worker('worker-a')], helpers_has_more: true })
    render(<SidebarHelpers parent={parent} {...actions()} />)
    fireEvent.click(screen.getByText('Load more'))
    await waitFor(() => expect(screen.getByText('worker-b')).toBeTruthy())
    expect(listSessionHelpers).toHaveBeenCalledWith(parent, 1)
    expect(screen.queryByText('Load more')).toBeNull()
    expect(screen.getByText('Helpers (2)')).toBeTruthy()
  })
})
