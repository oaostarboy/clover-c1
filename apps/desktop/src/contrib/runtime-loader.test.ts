import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as CloverModule from '@/clover'
import type { CloverReadDirResult } from '@/global'

import { $pluginRecords, publishPlugin, setPluginEnabled } from './plugins-store'
import { discoverRuntimePlugins, loadRuntimePlugin, watchRuntimePlugins } from './runtime-loader'

// getStatus would supply the connected backend's clover_home — a REMOTE path in
// remote mode. The disk scanner must NOT derive the plugin root from it (#66899).
const getStatus = vi.fn(async () => ({ clover_home: '/remote/box/.clover' }))

vi.mock('@/clover', async importActual => ({
  ...(await importActual<typeof CloverModule>()),
  getStatus: () => getStatus()
}))

const desktopPluginsRoot = vi.fn<() => Promise<string>>()
const agentPluginsRoot = vi.fn<() => Promise<string>>()
const readDir = vi.fn<(path: string) => Promise<CloverReadDirResult>>()
const readFileText = vi.fn<(path: string) => Promise<{ text: string; truncated?: boolean }>>()
const readPluginSource = vi.fn<(path: string) => Promise<{ text: string; truncated?: boolean }>>()
const watchDirectory = vi.fn<(path: string) => Promise<{ id: string }>>()
const watchPreviewFile = vi.fn<(path: string) => Promise<{ id: string }>>()
const stopPreviewFileWatch = vi.fn<(id: string) => Promise<boolean>>()
const onPreviewFileChanged = vi.fn()

beforeEach(() => {
  desktopPluginsRoot.mockReset()
  agentPluginsRoot.mockReset()
  readDir.mockReset()
  readFileText.mockReset()
  readPluginSource.mockReset()
  watchDirectory.mockReset()
  watchPreviewFile.mockReset()
  stopPreviewFileWatch.mockReset()
  stopPreviewFileWatch.mockResolvedValue(true)
  onPreviewFileChanged.mockReset()
  getStatus.mockClear()
  ;(window as unknown as { cloverDesktop: unknown }).cloverDesktop = {
    agentPluginsRoot,
    desktopPluginsRoot,
    onPreviewFileChanged,
    readDir,
    readFileText,
    stopPreviewFileWatch,
    watchDirectory,
    watchPreviewFile
  }
})

afterEach(() => {
  delete (window as unknown as { cloverDesktop?: unknown }).cloverDesktop
})

describe('scanDiskPlugins (#66899)', () => {
  it('scans the Electron-resolved local roots, never the backend clover_home', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.clover/plugins')
    readDir.mockResolvedValue({ entries: [] })

    await discoverRuntimePlugins()

    expect(desktopPluginsRoot).toHaveBeenCalled()
    expect(readDir).toHaveBeenCalledWith('/local/.clover/desktop-plugins')
    expect(readDir).toHaveBeenCalledWith('/local/.clover/plugins')
    // The remote backend's clover_home must never feed the local plugin scan.
    expect(getStatus).not.toHaveBeenCalled()
    expect(readDir).not.toHaveBeenCalledWith('/remote/box/.clover/desktop-plugins')
  })

  it('no-ops when the resolvers yield no local root', async () => {
    desktopPluginsRoot.mockResolvedValue('')
    agentPluginsRoot.mockResolvedValue('')

    await discoverRuntimePlugins()

    expect(readDir).not.toHaveBeenCalled()
  })

  it('treats a package without a Desktop half as metadata, not a throwing file read', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.clover/plugins')
    readDir.mockImplementation(async dir => {
      if (dir === '/local/.clover/plugins') {
        return { entries: [{ isDirectory: true, name: 'my-feature', path: '/local/.clover/plugins/my-feature' }] }
      }

      if (dir === '/local/.clover/plugins/my-feature') {
        return {
          entries: [{ isDirectory: false, name: 'plugin.yaml', path: '/local/.clover/plugins/my-feature/plugin.yaml' }]
        }
      }

      return { entries: [] }
    })

    await discoverRuntimePlugins()

    expect(readDir).toHaveBeenCalledWith('/local/.clover/plugins/my-feature')
    expect(readDir).not.toHaveBeenCalledWith('/local/.clover/plugins/my-feature/desktop')
    expect(readFileText).not.toHaveBeenCalled()
  })

  it('a DIRECTORY named plugin.js is not a plugin entry (metadata walk rejects it)', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('')
    readDir.mockImplementation(async dir => {
      if (dir === '/local/.clover/desktop-plugins') {
        return { entries: [{ isDirectory: true, name: 'odd', path: '/local/.clover/desktop-plugins/odd' }] }
      }

      if (dir === '/local/.clover/desktop-plugins/odd') {
        // A folder literally named plugin.js — must resolve to "no entry".
        return {
          entries: [{ isDirectory: true, name: 'plugin.js', path: '/local/.clover/desktop-plugins/odd/plugin.js' }]
        }
      }

      return { entries: [] }
    })

    await discoverRuntimePlugins()

    expect(readFileText).not.toHaveBeenCalled()
    expect($pluginRecords.get().odd).toBeUndefined()
  })

  it('still scans the standalone root when agentPluginsRoot is absent (older shell)', async () => {
    delete (window.cloverDesktop as unknown as { agentPluginsRoot?: unknown }).agentPluginsRoot
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    readDir.mockResolvedValue({ entries: [] })

    await discoverRuntimePlugins()

    expect(readDir).toHaveBeenCalledWith('/local/.clover/desktop-plugins')
    expect(readDir).toHaveBeenCalledTimes(1)
  })

  it('loads a unified desktop half OPT-IN: inventoried but not activated by default', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.clover/plugins')
    let desktopEntryPresent = true

    readDir.mockImplementation(async dir => {
      if (dir === '/local/.clover/plugins') {
        return { entries: [{ isDirectory: true, name: 'uni', path: '/local/.clover/plugins/uni' }] }
      }

      if (dir === '/local/.clover/plugins/uni') {
        return {
          entries: [{ isDirectory: true, name: 'desktop', path: '/local/.clover/plugins/uni/desktop' }]
        }
      }

      if (dir === '/local/.clover/plugins/uni/desktop') {
        return {
          entries: desktopEntryPresent
            ? [
                {
                  isDirectory: false,
                  name: 'plugin.js',
                  path: '/local/.clover/plugins/uni/desktop/plugin.js'
                }
              ]
            : []
        }
      }

      return { entries: [] }
    })

    const register = vi.fn()

    ;(globalThis as unknown as { __uniRegister: unknown }).__uniRegister = register
    readFileText.mockResolvedValue({
      text: 'export default { id: "uni", register: globalThis.__uniRegister }'
    })
    watchPreviewFile.mockResolvedValue({ id: 'w-uni' })

    // The loader evaluates plugins via blob-URL import(), which vite's module
    // runner can't resolve in tests — reroute to a data: URL, which node's
    // native ESM loader handles.
    const createObjectURL = vi
      .spyOn(URL, 'createObjectURL')
      .mockImplementation(
        blob =>
          `data:text/javascript;base64,${Buffer.from((blob as unknown as { parts: string[] }).parts.join('')).toString('base64')}`
      )

    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const RealBlob = globalThis.Blob
    vi.stubGlobal(
      'Blob',
      class {
        parts: string[]
        constructor(parts: string[]) {
          this.parts = parts
        }
      }
    )

    try {
      await discoverRuntimePlugins()

      // Inventoried for Settings → Plugins, but the root's opt-in posture wins:
      // ~/.clover/plugins stays installed-but-inert until the user toggles it.
      expect($pluginRecords.get().uni).toMatchObject({ kind: 'disk', status: 'disabled' })
      expect(register).not.toHaveBeenCalled()

      // The user's explicit enable still activates it.
      await setPluginEnabled('uni', true)
      expect(register).toHaveBeenCalledTimes(1)
      expect($pluginRecords.get().uni.status).toBe('loaded')

      // Removing only desktop/plugin.js (while the Python package folder
      // remains) unloads the previous Desktop registration instead of leaving
      // a live ghost behind.
      desktopEntryPresent = false
      await discoverRuntimePlugins()
      expect($pluginRecords.get().uni).toBeUndefined()
      expect(stopPreviewFileWatch).toHaveBeenCalledWith('w-uni')
    } finally {
      createObjectURL.mockRestore()
      revokeObjectURL.mockRestore()
      vi.stubGlobal('Blob', RealBlob)
      delete (globalThis as unknown as { __uniRegister?: unknown }).__uniRegister
    }
  })
})

describe('watchRuntimePlugins dir watch (#66899)', () => {
  it('watches both Electron-resolved local roots, never the backend clover_home', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.clover/plugins')
    readDir.mockResolvedValue({ entries: [] })
    watchDirectory.mockResolvedValue({ id: 'watch-1' })

    watchRuntimePlugins()
    // Drain the async scan + startDirWatches chains.
    await vi.waitFor(() => expect(watchDirectory).toHaveBeenCalledTimes(2))

    expect(watchDirectory).toHaveBeenCalledWith('/local/.clover/desktop-plugins')
    expect(watchDirectory).toHaveBeenCalledWith('/local/.clover/plugins')
    expect(watchDirectory).not.toHaveBeenCalledWith('/remote/box/.clover/desktop-plugins')
    expect(getStatus).not.toHaveBeenCalled()
  })
})

describe('plugin source reads (512 KiB preview-cap bug)', () => {
  const blobToDataUrl = () => {
    const createObjectURL = vi
      .spyOn(URL, 'createObjectURL')
      .mockImplementation(
        blob =>
          `data:text/javascript;base64,${Buffer.from((blob as unknown as { parts: string[] }).parts.join('')).toString('base64')}`
      )

    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const RealBlob = globalThis.Blob
    vi.stubGlobal(
      'Blob',
      class {
        parts: string[]
        constructor(parts: string[]) {
          this.parts = parts
        }
      }
    )

    return () => {
      createObjectURL.mockRestore()
      revokeObjectURL.mockRestore()
      vi.stubGlobal('Blob', RealBlob)
    }
  }

  /** Two-level standalone-root listing the metadata-walk probe needs:
   *  the root lists the package folder, the folder lists plugin.js. */
  const standaloneRootWith = (name: string) => {
    const folder = `/local/.clover/desktop-plugins/${name}`

    readDir.mockImplementation(async dir => {
      if (dir === '/local/.clover/desktop-plugins') {
        return { entries: [{ isDirectory: true, name, path: folder }] }
      }

      if (dir === folder) {
        return { entries: [{ isDirectory: false, name: 'plugin.js', path: `${folder}/plugin.js` }] }
      }

      return { entries: [] }
    })
  }

  it('loads the full source via readPluginSource when the shell offers it', async () => {
    ;(window.cloverDesktop as unknown as { readPluginSource: unknown }).readPluginSource = readPluginSource
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('')
    standaloneRootWith('big')
    // The preview read would truncate this source — it must never be used.
    readFileText.mockResolvedValue({ text: '// first 512 KiB only', truncated: true })

    const register = vi.fn()

    ;(globalThis as unknown as { __bigRegister: unknown }).__bigRegister = register
    readPluginSource.mockResolvedValue({
      text: 'export default { id: "big", register: globalThis.__bigRegister }'
    })
    watchPreviewFile.mockResolvedValue({ id: 'w-big' })

    const restore = blobToDataUrl()

    try {
      await discoverRuntimePlugins()

      // The EVALUATED source came from the full read, not the truncated preview.
      expect(readPluginSource).toHaveBeenCalledWith('/local/.clover/desktop-plugins/big/plugin.js')
      expect(register).toHaveBeenCalledTimes(1)
      expect($pluginRecords.get().big).toMatchObject({ kind: 'disk', status: 'loaded' })
    } finally {
      restore()
      delete (globalThis as unknown as { __bigRegister?: unknown }).__bigRegister
    }
  })

  it('older shell without readPluginSource: a truncated preview read fails LOUDLY, never evaluates', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('')
    standaloneRootWith('huge')
    // 512 KiB window of a larger file — parses fine, but is NOT the plugin.
    readFileText.mockResolvedValue({
      text: 'export default { id: "huge", register: () => { throw new Error("must never evaluate") } }',
      truncated: true
    })
    watchPreviewFile.mockResolvedValue({ id: 'w-huge' })

    const restore = blobToDataUrl()

    try {
      await discoverRuntimePlugins()

      // No live plugin — an error inventory row names the folder instead.
      expect($pluginRecords.get().huge).toMatchObject({
        kind: 'disk',
        status: 'error',
        file: '/local/.clover/desktop-plugins/huge/plugin.js'
      })
      expect($pluginRecords.get().huge.error).toMatch(/512 KiB/)
    } finally {
      restore()
    }
  })

  it('older shell, small plugin (not truncated): still loads through readFileText', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.clover/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('')
    standaloneRootWith('small')

    const register = vi.fn()

    ;(globalThis as unknown as { __smallRegister: unknown }).__smallRegister = register
    readFileText.mockResolvedValue({
      text: 'export default { id: "small", register: globalThis.__smallRegister }'
    })
    watchPreviewFile.mockResolvedValue({ id: 'w-small' })

    const restore = blobToDataUrl()

    try {
      await discoverRuntimePlugins()

      expect(register).toHaveBeenCalledTimes(1)
      expect($pluginRecords.get().small).toMatchObject({ kind: 'disk', status: 'loaded' })
    } finally {
      restore()
      delete (globalThis as unknown as { __smallRegister?: unknown }).__smallRegister
    }
  })
})

describe('bundled-shadowed disk copies', () => {
  it('skips a disk copy of a bundled plugin but publishes a visible inventory row', async () => {
    // The bundled twin is already registered (build-time glob).
    publishPlugin({ id: 'clover-bots', name: 'Bot Mode', kind: 'bundled', status: 'loaded' })

    // Same blob→data: URL reroute as the opt-in test above.
    const createObjectURL = vi
      .spyOn(URL, 'createObjectURL')
      .mockImplementation(
        blob =>
          `data:text/javascript;base64,${Buffer.from((blob as unknown as { parts: string[] }).parts.join('')).toString('base64')}`
      )

    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const RealBlob = globalThis.Blob
    vi.stubGlobal(
      'Blob',
      class {
        parts: string[]
        constructor(parts: string[]) {
          this.parts = parts
        }
      }
    )

    try {
      const id = await loadRuntimePlugin(
        'export default { id: "clover-bots", name: "Bot Mode", register() {} }',
        'clover-bots',
        { file: '/local/.clover/desktop-plugins/clover-bots/plugin.js' }
      )

      // Skipped — the bundled copy stays the only live registration...
      expect(id).toBeNull()
      expect($pluginRecords.get()['clover-bots']).toMatchObject({ kind: 'bundled', status: 'loaded' })

      // ...but the stale folder is DISCOVERABLE: an inventory row names it,
      // carries its path (reveal/delete affordance), and can never activate.
      expect($pluginRecords.get()['clover-bots:disk-shadowed']).toMatchObject({
        kind: 'disk',
        status: 'disabled',
        file: '/local/.clover/desktop-plugins/clover-bots/plugin.js'
      })
    } finally {
      createObjectURL.mockRestore()
      revokeObjectURL.mockRestore()
      vi.stubGlobal('Blob', RealBlob)
    }
  })
})
