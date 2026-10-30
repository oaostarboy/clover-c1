import { beforeEach, describe, expect, it, vi } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

vi.mock('@/lib/gateway-rpc', () => ({ isMissingRestEndpoint: () => false }))
vi.mock('@/store/transcript-tail', () => ({ recordTranscriptTail: vi.fn() }))
vi.mock('./client', () => ({
  capabilityScoped: vi.fn(),
  getApiRequestConnection: vi.fn(() => 'prometheus'),
  cloverApi: vi.fn(),
  profileScoped: vi.fn(() => ({}))
}))

const client = await import('./client')
const { listSessionHelpers, listSidebarSessions } = await import('./sessions')

const cloverApi = vi.mocked(client.cloverApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue('prometheus')
})

describe('listSidebarSessions remote ownership', () => {
  it('pages helpers on the exact parent connection and profile', async () => {
    const parent = makeSessionInfo({ id: 'parent/tip', profile: 'worker', connection_id: 'prometheus' })
    vi.mocked(client.capabilityScoped).mockReturnValue({ connectionId: 'prometheus', profile: 'worker' })
    cloverApi.mockResolvedValue({ sessions: [], has_more: false, limit: 20, offset: 5 } as never)

    await listSessionHelpers(parent, 5)

    expect(client.capabilityScoped).toHaveBeenCalledWith({ connectionId: 'prometheus', profile: 'worker' })
    expect(cloverApi).toHaveBeenCalledWith({
      connectionId: 'prometheus',
      profile: 'worker',
      path: '/api/sessions/parent%2Ftip/helpers?limit=20&offset=5&profile=worker'
    })
  })

  it('stamps active remote rows so a later resume stays on their gateway', async () => {
    cloverApi.mockResolvedValue({
      cron: { sessions: [] },
      messaging: { sessions: [] },
      recents: {
        sessions: [{ id: 'remote-session', profile: 'default', source: 'desktop', title: 'Remote chat' }]
      }
    } as never)

    const result = await listSidebarSessions({
      recentsProfile: 'default',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(result.recents.sessions[0]).toMatchObject({ connection_id: 'prometheus', id: 'remote-session' })
  })
})
