import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import os from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'

import { test, vi } from 'vitest'

import {
  buildPinArgs,
  buildPosixPinArgs,
  cachedScriptPath,
  hasExistingGitCheckout,
  installedAgentInstallScript,
  installRefForStamp,
  isPinnedCommit,
  resolveInstallScript,
  resolveMarkerPinnedCommit,
  runBootstrap
} from './bootstrap-runner'

test('real downloader fetches the pinned installer from the project raw URL', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-url-'))
  let url = ''

  const spy = vi.spyOn(https, 'get').mockImplementation(((requested, callback) => {
    url = String(requested)
    const request = new EventEmitter()
    queueMicrotask(() => {
      const response = new PassThrough() as PassThrough & { statusCode: number }
      response.statusCode = 200
      ;(callback as (res: unknown) => void)(response)
      response.end('test installer')
    })

    return request
  }) as typeof https.get)

  try {
    const sha = 'a'.repeat(40)
    const result = await resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })
    assert.equal(url, `https://raw.githubusercontent.com/oaostarboy/clover-c1/${sha}/scripts/${SCRIPT_NAME}`)
    assert.equal(fs.readFileSync(result.path, 'utf8'), 'test installer')
  } finally {
    spy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('interrupted response rejects and removes partial installer', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-interrupted-'))

  const spy = vi.spyOn(https, 'get').mockImplementation(((_url: unknown, callback: (response: unknown) => void) => {
    const request = new EventEmitter()
    queueMicrotask(() => {
      const response = new PassThrough() as PassThrough & { statusCode: number }
      response.statusCode = 200
      callback(response)
      response.write('partial')
      response.destroy(new Error('connection interrupted'))
    })

    return request
  }) as typeof https.get)

  try {
    const sha = 'c'.repeat(40)
    const result = resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })
    await assert.rejects(Promise.race([result, new Promise((_, reject) => setTimeout(() => reject(new Error('download remained pending')), 300))]), /connection interrupted|premature close/i)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
  } finally {
    spy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

for (const fault of ['abort', 'premature-close', 'redirect-abort', 'rename'] as const) {
  test(`downloader cleans up ${fault} and permits a successful retry`, async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-lifecycle-'))
    const sha = 'b'.repeat(40)
    let failing = true
    let requests = 0

    const rename = fault === 'rename'
      ? vi.spyOn(fs, 'renameSync').mockImplementation(() => { throw new Error('rename refused') })
      : null

    const spy = vi.spyOn(https, 'get').mockImplementation(((_url: unknown, callback: (response: unknown) => void) => {
      const request = new EventEmitter()
      requests++
      const redirect = failing && fault === 'redirect-abort' && requests === 1
      queueMicrotask(() => {
        const response = Object.assign(new PassThrough(), {
          statusCode: redirect ? 302 : 200,
          headers: redirect ? { location: 'https://example.test/installer' } : {}
        })

        callback(response)

        if (redirect) {return}

        if (!failing || fault === 'rename') {
          response.end('complete installer')
        } else {
          response.write('partial')

          if (fault !== 'premature-close') {response.emit('aborted')}
          response.destroy()
        }
      })

      return request
    }) as typeof https.get)

    const attempt = () => resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })

    try {
      await assert.rejects(attempt(), /interrupted|premature close|rename refused/i)
      assert.equal(fs.existsSync(cachedScriptPath(home, sha)), false)
      assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
      rename?.mockRestore()
      failing = false
      const result = await attempt()
      assert.equal(fs.readFileSync(result.path, 'utf8'), 'complete installer')
    } finally {
      rename?.mockRestore()
      spy.mockRestore()
      fs.rmSync(home, { recursive: true, force: true })
    }
  })
}

for (const headersReceived of [false, true]) {
  test(`stalled download times out with headers=${headersReceived}`, async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-timeout-'))
    const sha = 'e'.repeat(40)
    const request = Object.assign(new EventEmitter(), { destroy: vi.fn() })
    let response: PassThrough | undefined
    let markRequested!: () => void
    const requested = new Promise<void>(resolve => { markRequested = resolve })

    const spy = vi.spyOn(https, 'get').mockImplementation(((_url: unknown, callback: (response: unknown) => void) => {
      markRequested()

      if (headersReceived) {
        queueMicrotask(() => {
          response = Object.assign(new PassThrough(), { statusCode: 200 })
          callback(response)
          response.write('partial')
        })
      }

      return request
    }) as unknown as typeof https.get)

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    try {
      const attempt = resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })
      const rejection = assert.rejects(attempt, /timed out/)
      await requested
      await vi.advanceTimersByTimeAsync(60_001)
      await rejection
      assert.equal(request.destroy.mock.calls.length, 1)

      if (response) {assert.equal(response.destroyed, true)}
      assert.equal(fs.existsSync(cachedScriptPath(home, sha)), false)
      assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
      assert.equal(vi.getTimerCount(), 0)
    } finally {
      vi.useRealTimers()
      spy.mockRestore()
      fs.rmSync(home, { recursive: true, force: true })
    }
  })
}

test('writer failure removes partial file and retry remains intact', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-writer-'))
  const sha = '9'.repeat(40)
  const create = fs.createWriteStream
  let failing = true

  const writer = vi.spyOn(fs, 'createWriteStream').mockImplementation(((...args: Parameters<typeof fs.createWriteStream>) => {
    const out = create(...args)

    if (failing) {out.once('open', () => out.destroy(new Error('writer failed')))}

    return out
  }) as typeof fs.createWriteStream)

  const get = vi.spyOn(https, 'get').mockImplementation(((_url: unknown, callback: (response: unknown) => void) => {
    const request = new EventEmitter()
    queueMicrotask(() => {
      const response = Object.assign(new PassThrough(), { statusCode: 200 })
      callback(response)
      response.end('complete installer')
    })

    return request
  }) as unknown as typeof https.get)

  const attempt = () => resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })

  try {
    await assert.rejects(attempt(), /writer failed/)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha)), false)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
    failing = false
    const result = await attempt()
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.equal(fs.readFileSync(result.path, 'utf8'), 'complete installer')
    assert.equal(fs.existsSync(result.path + '.tmp'), false)
  } finally {
    writer.mockRestore()
    get.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('timeout remains absolute across redirects and permits retry', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-redirect-timeout-'))
  const sha = '8'.repeat(40)
  let calls = 0
  let notify!: () => void
  const started = new Promise<void>(resolve => { notify = resolve })
  const requests: Array<EventEmitter & { destroy: ReturnType<typeof vi.fn> }> = []

  const get = vi.spyOn(https, 'get').mockImplementation(((_url: unknown, callback: (response: unknown) => void) => {
    const request = Object.assign(new EventEmitter(), { destroy: vi.fn() })
    requests.push(request)
    calls++

    if (calls === 1) {notify()}

    if (calls === 3) {queueMicrotask(() => callback(Object.assign(new PassThrough(), { statusCode: 200 }).end('complete installer')))}

    return request
  }) as unknown as typeof https.get)

  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const attempt = () => resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })

  try {
    const pending = attempt()
    const rejection = assert.rejects(pending, /timed out/)
    await started
    await vi.advanceTimersByTimeAsync(59_000)
    // Deliver a redirect near the original deadline.
    const callback = get.mock.calls[0][1] as (response: unknown) => void
    callback(Object.assign(new PassThrough(), { statusCode: 302, headers: { location: 'https://example.test/install' } }))
    assert.equal(calls, 2)
    await vi.advanceTimersByTimeAsync(1_001)
    await rejection
    assert.equal(requests[1].destroy.mock.calls.length, 1)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
    vi.useRealTimers()
    const result = await attempt()
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.equal(fs.readFileSync(result.path, 'utf8'), 'complete installer')
    assert.equal(fs.existsSync(result.path + '.tmp'), false)
  } finally {
    vi.useRealTimers()
    get.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

const SCRIPT_NAME = process.platform === 'win32' ? 'install.ps1' : 'install.sh'
const ZERO_COMMIT = '0000000000000000000000000000000000000000'

test('real socket reset removes partial download and permits retry', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bootstrap-socket-'))
  const sha = 'f'.repeat(40)
  let calls = 0

  const server = http.createServer((_req, res) => {
    calls++

    if (calls === 1) {
      res.writeHead(200, { 'Content-Length': '100' })
      res.write('partial')
      setTimeout(() => res.socket?.destroy(), 10)
    } else {
      res.end('complete installer')
    }
  })

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  assert.ok(address && typeof address !== 'string')

  const spy = vi.spyOn(https, 'get').mockImplementation(((requested, callback) => {
    assert.equal(String(requested), `https://raw.githubusercontent.com/oaostarboy/clover-c1/${sha}/scripts/${SCRIPT_NAME}`)

    return http.get(`http://127.0.0.1:${address.port}/`, callback)
  }) as typeof https.get)

  const attempt = () => resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })

  try {
    await assert.rejects(attempt())
    assert.equal(fs.existsSync(cachedScriptPath(home, sha)), false)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
    const result = await attempt()
    assert.equal(fs.readFileSync(result.path, 'utf8'), 'complete installer')
    assert.equal(calls, 2)
  } finally {
    spy.mockRestore()
    await new Promise<void>(resolve => server.close(() => resolve()))
    fs.rmSync(home, { recursive: true, force: true })
  }
})

function mkTmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'clover-bootstrap-test-'))
}

test('fallback branch downloads using the Windows installer path', async () => {
  const home = mkTmpHome()
  const original = process.platform
  let url = ''
  Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })

  const spy = vi.spyOn(https, 'get').mockImplementation(((requested, callback) => {
    url = String(requested)
    const request = new EventEmitter()
    queueMicrotask(() => {
      const response = new PassThrough() as PassThrough & { statusCode: number }
      response.statusCode = 200
      ;(callback as (res: unknown) => void)(response)
      response.end('test installer')
    })

    return request
  }) as typeof https.get)

  try {
    const result = await resolveInstallScript({ installStamp: { commit: ZERO_COMMIT, branch: 'release/test' }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })
    assert.equal(url, 'https://raw.githubusercontent.com/oaostarboy/clover-c1/release/test/scripts/install.ps1')
    assert.equal(result.commit, null)
    assert.equal(fs.readFileSync(result.path, 'utf8'), 'test installer')
  } finally {
    spy.mockRestore()
    Object.defineProperty(process, 'platform', { value: original, configurable: true })
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('HTTP error does not create a usable cached installer', async () => {
  const home = mkTmpHome()
  let responseStream: PassThrough | undefined
  let output: fs.WriteStream | undefined
  const originalCreateWriteStream = fs.createWriteStream

  const outputSpy = vi.spyOn(fs, 'createWriteStream').mockImplementation(((...args: Parameters<typeof fs.createWriteStream>) => {
    output = originalCreateWriteStream(...args)

    return output!
  }) as typeof fs.createWriteStream)

  const spy = vi.spyOn(https, 'get').mockImplementation(((requested, callback) => {
    const request = new EventEmitter()
    queueMicrotask(() => {
      const response = new PassThrough() as PassThrough & { statusCode: number }
      responseStream = response
      response.statusCode = 404
      ;(callback as (res: unknown) => void)(response)
    })

    return request
  }) as typeof https.get)

  try {
    const sha = 'b'.repeat(40)
    await assert.rejects(resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} }), /HTTP 404/)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha)), false)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
    // A failure before HTTP 200 need not open an output file at all.
    assert.ok(output === undefined || output.closed)
    assert.equal(responseStream?.destroyed, true)
  } finally {
    spy.mockRestore()
    outputSpy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('request error closes output before rejecting', async () => {
  const home = mkTmpHome()
  let output: fs.WriteStream | undefined
  const create = fs.createWriteStream
  const outputSpy = vi.spyOn(fs, 'createWriteStream').mockImplementation(((...args: Parameters<typeof fs.createWriteStream>) => (output = create(...args))) as typeof fs.createWriteStream)

  const spy = vi.spyOn(https, 'get').mockImplementation((() => {
    const request = new EventEmitter()
    queueMicrotask(() => request.emit('error', new Error('connection failed')))

    return request
  }) as unknown as typeof https.get)

  try {
    const sha = 'd'.repeat(40)
    await assert.rejects(resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} }), /connection failed/)
    // A failure before HTTP 200 need not open an output file at all.
    assert.ok(output === undefined || output.closed)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
  } finally {
    spy.mockRestore()
    outputSpy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('redirected download saves completed installer', async () => {
  const home = mkTmpHome()
  let calls = 0

  const spy = vi.spyOn(https, 'get').mockImplementation(((requested, callback) => {
    const request = new EventEmitter()
    const call = calls++
    queueMicrotask(() => {
      const response = new PassThrough() as PassThrough & { statusCode: number, headers: { location: string } }
      response.statusCode = call === 0 ? 302 : 200
      response.headers = { location: 'https://example.test/installer' }
      ;(callback as (res: unknown) => void)(response)

      if (call !== 0) {response.end('redirect installer')}
    })

    return request
  }) as typeof https.get)

  try {
    const result = await resolveInstallScript({ installStamp: { commit: 'e'.repeat(40) }, sourceRepoRoot: null, cloverHome: home, emit: () => {} })
    assert.equal(calls, 2)
    assert.equal(fs.readFileSync(result.path, 'utf8'), 'redirect installer')
  } finally {
    spy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('redirected HTTP failure closes the response and leaves no temporary file', async () => {
  const home = mkTmpHome()
  const responses: PassThrough[] = []
  let calls = 0

  const spy = vi.spyOn(https, 'get').mockImplementation(((requested, callback) => {
    const request = new EventEmitter()
    const call = calls++
    queueMicrotask(() => {
      const response = new PassThrough() as PassThrough & { statusCode: number, headers: { location: string } }
      response.statusCode = call === 0 ? 302 : 404
      response.headers = { location: 'https://example.test/installer' }
      responses.push(response)
      ;(callback as (res: unknown) => void)(response)
    })

    return request
  }) as typeof https.get)

  try {
    const sha = 'c'.repeat(40)
    await assert.rejects(resolveInstallScript({ installStamp: { commit: sha }, sourceRepoRoot: null, cloverHome: home, emit: () => {} }), /HTTP 404/)
    assert.equal(calls, 2)
    assert.ok(responses.every(response => response.destroyed))
    assert.equal(fs.existsSync(cachedScriptPath(home, sha)), false)
    assert.equal(fs.existsSync(cachedScriptPath(home, sha) + '.tmp'), false)
  } finally {
    spy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('fallback branch cache keys distinguish slash from underscore', async () => {
  const home = mkTmpHome()

  try {
    const resolve = (branch: string) => resolveInstallScript({ installStamp: { commit: ZERO_COMMIT, branch }, sourceRepoRoot: null, cloverHome: home, emit: () => {}, _download: async (ref, dest) => { fs.mkdirSync(path.dirname(dest), { recursive: true }); fs.writeFileSync(dest, ref) } })
    const first = await resolve('release/test')
    const second = await resolve('release_test')
    assert.notEqual(first.path, second.path)
    assert.equal(fs.readFileSync(second.path, 'utf8'), 'release_test')
  } finally { fs.rmSync(home, { recursive: true, force: true }) }
})

test('fallback refs cannot inject URL syntax', async () => {
  const home = mkTmpHome()
  const spy = vi.spyOn(https, 'get')

  try {
    const invalid = 'https://evil/x'
    const cache = cachedScriptPath(home, `fallback-${encodeURIComponent(invalid)}`)
    fs.mkdirSync(path.dirname(cache), { recursive: true })
    fs.writeFileSync(cache, 'must not execute')
    await assert.rejects(resolveInstallScript({ installStamp: { commit: ZERO_COMMIT, branch: invalid }, sourceRepoRoot: null, cloverHome: home, emit: () => {} }), /Invalid installer ref/)
    assert.equal(spy.mock.calls.length, 0)
  } finally {
    spy.mockRestore()
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('runBootstrap bails immediately when the signal is already aborted', async () => {
  const controller = new AbortController()
  controller.abort()

  const events = []

  const result = await runBootstrap({
    installStamp: null,
    activeRoot: '/tmp/clover-runner-test',
    sourceRepoRoot: null,
    cloverHome: '/tmp/clover-runner-test',
    logRoot: '/tmp/clover-runner-test',
    onEvent: ev => events.push(ev),
    abortSignal: controller.signal
  })

  // Cancelled before any install script is spawned.
  assert.deepEqual(result, { ok: false, cancelled: true })
  assert.ok(
    events.some(ev => ev.type === 'failed' && /cancelled/i.test(ev.error)),
    'should emit a cancelled failure event'
  )
})

test('installedAgentInstallScript resolves the installer in the agent checkout', () => {
  const home = mkTmpHome()

  try {
    assert.equal(installedAgentInstallScript(home), null, 'absent before the checkout exists')

    const scriptsDir = path.join(home, 'clover-c1', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const scriptPath = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(scriptPath, '#!/bin/sh\necho hi\n')

    assert.equal(installedAgentInstallScript(home), scriptPath)
    assert.equal(installedAgentInstallScript(null), null, 'null home -> null')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('existing checkout detection requires git metadata', () => {
  const home = mkTmpHome()

  try {
    const activeRoot = path.join(home, 'clover-c1')
    assert.equal(hasExistingGitCheckout(activeRoot), false)

    fs.mkdirSync(path.join(activeRoot, '.git'), { recursive: true })
    assert.equal(hasExistingGitCheckout(activeRoot), true)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('fresh bootstrap args include the packaged commit pin', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main' }

  assert.deepEqual(buildPinArgs(installStamp), ['-Commit', installStamp.commit, '-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/clover-c1',
      cloverHome: '/tmp/clover'
    }),
    ['--dir', '/tmp/clover-c1', '--clover-home', '/tmp/clover', '--branch', 'main', '--commit', installStamp.commit]
  )
})

test('existing-checkout bootstrap args keep branch but skip the packaged commit pin', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main' }

  assert.deepEqual(buildPinArgs(installStamp, { pinCommit: false }), ['-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/clover-c1',
      cloverHome: '/tmp/clover',
      pinCommit: false
    }),
    ['--dir', '/tmp/clover-c1', '--clover-home', '/tmp/clover', '--branch', 'main']
  )
})

test('fallback install stamps use an unpinned branch ref', () => {
  const stamp = { commit: ZERO_COMMIT, branch: 'main' }

  assert.equal(isPinnedCommit(ZERO_COMMIT), false)
  assert.deepEqual(installRefForStamp(stamp), {
    ref: 'main',
    cacheKey: 'fallback-main',
    pinned: false
  })
  // Must NOT pass -Commit / --commit for the all-zero placeholder.
  assert.deepEqual(buildPinArgs(stamp), ['-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp: stamp,
      activeRoot: '/tmp/clover',
      cloverHome: '/tmp/home'
    }),
    ['--dir', '/tmp/clover', '--clover-home', '/tmp/home', '--branch', 'main']
  )
})

test('resolveMarkerPinnedCommit prefers real HEAD over fallback stamp zeros', () => {
  const realHead = 'c'.repeat(40)
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    realHead
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: 'd'.repeat(40), branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    'd'.repeat(40),
    'packaged real pin wins over checkout HEAD'
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/missing', {
      resolveHead: () => null
    }),
    null
  )
})

test('resolveInstallScript downloads fallback stamps by branch instead of zero commit', async () => {
  const home = mkTmpHome()

  try {
    const logs = []
    const refs = []

    const result = await resolveInstallScript({
      installStamp: { commit: ZERO_COMMIT, branch: 'main' },
      sourceRepoRoot: null,
      cloverHome: home,
      emit: ev => logs.push(ev),
      _download: async (ref, destPath) => {
        refs.push(ref)
        fs.mkdirSync(path.dirname(destPath), { recursive: true })
        fs.writeFileSync(destPath, '#!/bin/sh\necho fallback branch\n')

        return destPath
      }
    })

    assert.deepEqual(refs, ['main'])
    assert.equal(result.source, 'download')
    assert.equal(result.commit, null)
    assert.equal(result.path, cachedScriptPath(home, 'fallback-main'))
    assert.ok(
      logs.some(ev => /fallback, unpinned/.test(ev.line || '')),
      'emits an unpinned fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript prefers a cached script without touching the network', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    const cached = cachedScriptPath(home, commit)
    fs.mkdirSync(path.dirname(cached), { recursive: true })
    fs.writeFileSync(cached, '#!/bin/sh\necho cached\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      cloverHome: home,
      emit: ev => logs.push(ev)
    })

    assert.equal(result.source, 'cache')
    assert.equal(result.path, cached)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript falls back to the installed agent checkout on a 404', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // Seed the installed agent checkout so the fallback has something to resolve.
    const scriptsDir = path.join(home, 'clover-c1', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const installed = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(installed, '#!/bin/sh\necho fallback\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      cloverHome: home,
      emit: ev => logs.push(ev),
      // Simulate GitHub returning a 404 for the pinned commit.
      _download: async () => {
        throw new Error('Failed to download install.sh: HTTP 404')
      }
    })

    assert.equal(result.source, 'installed-agent')
    // It should have copied the installer into the bootstrap cache.
    assert.equal(result.path, cachedScriptPath(home, commit))
    assert.ok(fs.existsSync(result.path), 'fallback script copied into cache')
    assert.ok(
      logs.some(ev => /falling back to installed agent/.test(ev.line || '')),
      'emits a fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript rethrows when the 404 fallback is unavailable', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // No installed agent checkout seeded -> nothing to fall back to.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        cloverHome: home,
        emit: () => {},
        _download: async () => {
          throw new Error('Failed to download install.sh: HTTP 404')
        }
      }),
      /HTTP 404|Failed to download/
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
