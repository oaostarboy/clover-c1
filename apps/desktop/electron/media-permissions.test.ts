import { describe, expect, it } from 'vitest'

import { isDesktopPermissionRequestAllowed } from './media-permissions'

describe('desktop permission requests', () => {
  it('allows a user-initiated fullscreen request without granting automatic fullscreen', () => {
    expect(isDesktopPermissionRequestAllowed('fullscreen', {})).toBe(true)
    expect(isDesktopPermissionRequestAllowed('automatic-fullscreen', {})).toBe(false)
  })

  it('preserves capture behavior and denies unrelated permissions', () => {
    expect(isDesktopPermissionRequestAllowed('media', {})).toBe(true)
    expect(isDesktopPermissionRequestAllowed('media', { mediaTypes: ['audio'] })).toBe(true)
    expect(isDesktopPermissionRequestAllowed('media', { mediaTypes: ['screen'] })).toBe(false)
    expect(isDesktopPermissionRequestAllowed('geolocation', {})).toBe(false)
  })
})
