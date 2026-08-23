import { describe, expect, it } from 'vitest'

import {
  normalizeCloverOpenString,
  pathFromCloverDeepLink,
  pathFromOpenDeepLink,
  resolveCloverOpenPath
} from './clover-open-target'

describe('normalizeCloverOpenString', () => {
  it('accepts hash-router paths and strips a leading hash', () => {
    expect(normalizeCloverOpenString('/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeCloverOpenString('#/index-network/intent/1')).toBe('/index-network/intent/1')
  })

  it('maps plugin-scoped clover:// deep links to the same path', () => {
    expect(normalizeCloverOpenString('clover://index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeCloverOpenString('clover://index-network/intent/1?focus=true')).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('maps clover://open/… deep links by stripping the open host', () => {
    expect(normalizeCloverOpenString('clover://open/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeCloverOpenString('clover://open/settings/plugins')).toBe('/settings/plugins')
  })

  it('rejects reserved clover kinds and unsafe paths', () => {
    expect(normalizeCloverOpenString('clover://blueprint/morning-brief')).toBeNull()
    expect(normalizeCloverOpenString('clover://plugin/install')).toBeNull()
    expect(normalizeCloverOpenString('https://example.com/x')).toBeNull()
    expect(normalizeCloverOpenString('/../etc/passwd')).toBeNull()
    expect(normalizeCloverOpenString('index-network')).toBeNull()
  })
})

describe('resolveCloverOpenPath', () => {
  it('merges structured path + params', () => {
    expect(resolveCloverOpenPath({ path: '/index-network/intent/1', params: { focus: 'true' } })).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('resolves href the same as a bare string', () => {
    expect(resolveCloverOpenPath({ href: 'clover://index-network/intent/1' })).toBe('/index-network/intent/1')
  })
})

describe('pathFromCloverDeepLink', () => {
  it('builds the navigate path from a plugin-scoped deep-link payload', () => {
    expect(pathFromCloverDeepLink('index-network', 'intent/1')).toBe('/index-network/intent/1')
  })

  it('builds the navigate path from clover://open/… payloads', () => {
    expect(pathFromOpenDeepLink('index-network/intent/1')).toBe('/index-network/intent/1')
    expect(pathFromCloverDeepLink('open', 'agent/42')).toBe('/agent/42')
  })

  it('ignores reserved kinds', () => {
    expect(pathFromCloverDeepLink('blueprint', 'morning-brief')).toBeNull()
    expect(pathFromCloverDeepLink('plugin', 'install')).toBeNull()
  })
})
