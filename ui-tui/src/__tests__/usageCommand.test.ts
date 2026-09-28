import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import type { SessionUsageResponse } from '../gatewayTypes.js'

const usageCommand = sessionCommands.find(cmd => cmd.name === 'usage')!

const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

/** Build a ctx whose rpc routes by method name to a supplied map of results. */
const buildCtx = (results: Record<string, unknown>) => {
  const sys = vi.fn()
  const panel = vi.fn()

  const rpc = vi.fn((method: string, _params: unknown) => Promise.resolve(results[method]))

  const ctx = {
    gateway: { rpc },
    guarded,
    guardedErr: vi.fn(),
    sid: 'sid-1',
    stale: () => false,
    transcript: { page: vi.fn(), panel, sys }
  }

  const run = async (arg: string) => {
    usageCommand.run(arg, ctx as any, 'usage')
    await rpc.mock.results[0]?.value
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, panel, run, sys }
}

const baseUsage = (overrides: Partial<SessionUsageResponse> = {}): SessionUsageResponse =>
  ({ calls: 0, input: 0, output: 0, total: 0, ...overrides }) as SessionUsageResponse

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

const usagePanel = (panel: ReturnType<typeof vi.fn>) => panel.mock.calls.find(c => c[0] === 'Usage')?.[1]

describe('/usage slash command', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('help text advertises session token usage only', () => {
    expect(usageCommand.help).toBe('session token usage')
  })

  it('prints "no API calls yet" and no panel when there are no calls', async () => {
    const { panel, run, sys } = buildCtx({ 'session.usage': baseUsage({ calls: 0 }) })
    await run('')

    expect(printed(sys)).toContain('no API calls yet')
    expect(panel).not.toHaveBeenCalled()
  })

  it('renders a token-only Usage panel when there are calls', async () => {
    const { panel, run } = buildCtx({
      'session.usage': baseUsage({
        calls: 3,
        compressions: 1,
        context_max: 200000,
        context_percent: 12,
        context_used: 24000,
        input: 100,
        model: 'anthropic/claude-opus-4.6',
        output: 50,
        total: 150
      })
    })

    await run('')

    const sections = usagePanel(panel)
    expect(sections).toBeDefined()
    const rows = sections![0].rows as [string, string][]
    expect(rows).toContainEqual(['Model', 'anthropic/claude-opus-4.6'])
    expect(rows).toContainEqual(['Total tokens', '150'])
    expect(rows).toContainEqual(['API calls', '3'])

    const text = sections!.map(s => s.text ?? '').join('\n')
    expect(text).toContain('Context: 24,000 / 200,000 (12%)')
    expect(text).toContain('Compressions: 1')
  })
})
