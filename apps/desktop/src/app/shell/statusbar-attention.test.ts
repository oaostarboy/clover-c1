import { describe, expect, it } from 'vitest'

import { showGatewayHealth, showUpdateStatus } from './statusbar-attention'

describe('statusbar attention', () => {
  it('suppresses healthy gateway status while preserving disconnected, checking and restarting states', () => {
    expect(showGatewayHealth(false, true, false)).toBe(false)
    expect(showGatewayHealth(false, false, false)).toBe(true)
    expect(showGatewayHealth(false, true, true)).toBe(true)
  })

  it('shows updates and progress instead of a permanent version label', () => {
    expect(showUpdateStatus(false, false)).toBe(false)
    expect(showUpdateStatus(true, false)).toBe(true)
    expect(showUpdateStatus(false, true)).toBe(true)
  })
})
