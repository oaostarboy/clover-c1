import { describe, expect, it } from 'vitest'

import { resolvePluginSourceLinks } from './plugin-source-urls'

describe('resolvePluginSourceLinks', () => {
  it('maps owner/repo to github browse and clone urls', () => {
    expect(resolvePluginSourceLinks('Clover Cognition/clover-example-plugins')).toEqual({
      gitUrl: 'docs/plugins.md.git',
      browseUrl: 'docs/plugins.md',
      subdir: null
    })
  })

  it('includes monorepo subdir in browse url', () => {
    expect(resolvePluginSourceLinks('owner/repo/plugins/foo')).toEqual({
      gitUrl: 'https://github.com/owner/repo.git',
      browseUrl: 'https://github.com/owner/repo/tree/HEAD/plugins/foo',
      subdir: 'plugins/foo'
    })
  })

  it('supports git@ github urls', () => {
    expect(resolvePluginSourceLinks('git@github.com:owner/my-plugin.git')).toEqual({
      gitUrl: 'git@github.com:owner/my-plugin.git',
      browseUrl: 'https://github.com/owner/my-plugin',
      subdir: null
    })
  })

  it('returns null for invalid identifiers', () => {
    expect(resolvePluginSourceLinks('not-a-repo')).toBeNull()
  })
})
