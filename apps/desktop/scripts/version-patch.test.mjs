import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const original = path.dirname(require.resolve('app-builder-lib/package.json'))
const dependencyRoot = path.dirname(path.dirname(require.resolve('semver/package.json')))

test('builder patch upgrades the old coercion without changing valid versions', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'clover-builder-version-'))
  try {
    const scriptDir = path.join(temp, 'apps', 'desktop', 'scripts')
    const libDir = path.join(temp, 'node_modules', 'app-builder-lib')
    fs.mkdirSync(scriptDir, { recursive: true })
    fs.cpSync(original, libDir, { recursive: true, dereference: true })
    process.env.NODE_PATH = dependencyRoot
    require('node:module').Module._initPaths()
    const script = path.join(scriptDir, 'patch-electron-builder-mac-binary.mjs')
    fs.copyFileSync(path.join(import.meta.dirname, 'patch-electron-builder-mac-binary.mjs'), script)
    const normPath = path.join(libDir, 'out/util/normalizePackageData.js')
    const needle = 'if (!semver.valid(data.version, loose)) {'
    const legacy = 'const coerced = semver.coerce(data.version); if (coerced) { data.version = coerced.version; return true; } '
    const source = fs.readFileSync(normPath, 'utf8')
    if (!source.includes(legacy)) {
      assert.ok(source.includes(needle))
      fs.writeFileSync(normPath, source.replace(needle, legacy + needle))
    }
    const run = () => {
      const result = spawnSync(process.execPath, [script], { encoding: 'utf8' })
      assert.equal(result.status, 0, result.stderr)
    }
    run()
    const first = fs.readFileSync(normPath, 'utf8')
    assert.ok(!first.includes('data.version = coerced.version'))
    run()
    assert.equal(fs.readFileSync(normPath, 'utf8'), first)
    const norm = require(normPath)
    for (const version of ['2.0.2.3', '2.0.2.4', '2.0.2.10', '2.0.2', '2.0.2-beta.1']) {
      const metadata = { name: 'clover', version }
      norm.normalizePackageData(metadata)
      assert.equal(metadata.version, version)
    }
    for (const version of ['2.0.2.4.5', '2.0.2.x', '2.0.2.04']) {
      assert.throws(() => norm.normalizePackageData({ name: 'clover', version }), /Invalid version/)
    }
    const { AppInfo } = require(path.join(libDir, 'out/appInfo.js'))
    const info = new AppInfo({ metadata: { name: 'clover', version: '2.0.2.4' }, config: {} })
    assert.equal(info.version, '2.0.2.4')
    assert.equal(info.buildVersion, '2.0.2.4')
    assert.equal(info.channel, null)
  } finally { fs.rmSync(temp, { recursive: true, force: true }) }
})
