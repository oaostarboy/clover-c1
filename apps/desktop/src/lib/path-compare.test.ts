import { afterEach, describe, expect, it } from 'vitest'

import { cleanPath, comparisonPath, isUnderPath } from './path-compare'

// jsdom's default navigator.platform/userAgent never matches /mac/i (see
// src/store/translucency.test.ts), so these tests pin a Mac platform
// explicitly and restore it afterward to avoid leaking into other tests.
const pinPlatform = (platform: string) =>
  Object.defineProperty(globalThis.navigator, 'platform', { configurable: true, value: platform })

afterEach(() => {
  pinPlatform('')
})

describe('cleanPath', () => {
  it('unifies separators and drops trailing slashes', () => {
    expect(cleanPath('C:\\Repos\\App\\')).toBe('C:/Repos/App')
    expect(cleanPath('  /home/user/repo//  ')).toBe('/home/user/repo')
  })

  it('keeps root rather than collapsing to empty', () => {
    expect(cleanPath('/')).toBe('/')
  })
})

describe('comparisonPath', () => {
  it('folds case for Windows drive and UNC paths', () => {
    expect(comparisonPath('C:/Repos/App')).toBe('c:/repos/app')
    expect(comparisonPath('//server/Share')).toBe('//server/share')
  })

  it('stays case-sensitive on POSIX platforms other than macOS', () => {
    expect(comparisonPath('/home/User/Repo')).toBe('/home/User/Repo')
  })

  it('folds case on macOS, whose default volumes are case-insensitive', () => {
    pinPlatform('MacIntel')
    expect(comparisonPath('/Users/Alice/Repo')).toBe('/users/alice/repo')
  })
})

describe('isUnderPath', () => {
  it('matches a nested path across separator and case differences', () => {
    expect(isUnderPath('C:\\Repos\\App', 'c:/repos/app/src')).toBe(true)
    expect(isUnderPath('C:/Repos/App/', 'C:\\Repos\\App')).toBe(true)
  })

  it('stays case-sensitive on POSIX', () => {
    expect(isUnderPath('/home/user/repo', '/home/user/repo/src')).toBe(true)
    expect(isUnderPath('/home/user/repo', '/home/user/Repo/src')).toBe(false)
  })

  it('does not treat a sibling with a shared prefix as nested', () => {
    expect(isUnderPath('/repos/app', '/repos/app-retry')).toBe(false)
  })

  it('is case-insensitive under a macOS-style path', () => {
    pinPlatform('MacIntel')
    expect(isUnderPath('/Users/Alice/Repo', '/Users/Alice/repo/src')).toBe(true)
  })
})
