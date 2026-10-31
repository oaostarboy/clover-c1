import { test } from 'vitest'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { nativeVersionOptions } from './native-version-options.mjs'

test('runner sends exact metadata to its actual child and rejects conflicting inherited build number', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'builder-child-'))
  try {
    fs.mkdirSync(path.join(temp, 'scripts'), { recursive: true })
    fs.mkdirSync(path.join(temp, 'node_modules', 'electron-builder'), { recursive: true })
    for (const f of ['run-electron-builder.mjs', 'native-version-options.mjs']) fs.copyFileSync(path.join(import.meta.dirname, f), path.join(temp, 'scripts', f))
    fs.writeFileSync(path.join(temp, 'package.json'), JSON.stringify({ version: '2.0.2.4' }))
    fs.writeFileSync(path.join(temp, 'node_modules/electron-builder/package.json'), JSON.stringify({ bin: 'cli.cjs' }))
    fs.writeFileSync(path.join(temp, 'node_modules/electron-builder/cli.cjs'), "require('fs').writeFileSync(process.env.RECEIPT, JSON.stringify({ args: process.argv.slice(2), buildNumber: process.env.BUILD_NUMBER }))")
    const receipt = path.join(temp, 'receipt.json')
    const env = { ...process.env, RECEIPT: receipt }; delete env.BUILD_NUMBER
    const runner = path.join(temp, 'scripts/run-electron-builder.mjs')
    const run = spawnSync(process.execPath, [runner, '--win'], { env, encoding: 'utf8' })
    assert.equal(run.status, 0, run.stderr)
    const child = JSON.parse(fs.readFileSync(receipt, 'utf8'))
    assert.equal(child.buildNumber, '4')
    assert.ok(child.args.includes('-c.buildVersion=2.0.2.4'))
    for (const platform of ['--linux', '--mac']) {
      const result = spawnSync(process.execPath, [runner, platform, '-c.extraMetadata.example=kept'], { env, encoding: 'utf8' })
      assert.equal(result.status, 0, result.stderr)
      const platformChild = JSON.parse(fs.readFileSync(receipt, 'utf8'))
      assert.ok(platformChild.args.includes(platform))
      assert.ok(platformChild.args.includes('-c.extraMetadata.example=kept'))
      assert.equal(platformChild.buildNumber, '4')
    }
    const conflict = spawnSync(process.execPath, [runner, '--win'], { env: { ...env, BUILD_NUMBER: '9' }, encoding: 'utf8' })
    assert.notEqual(conflict.status, 0)
    for (const override of [ ['-c.buildNumber=9'], ['-c.buildVersion=9'], ['--config.buildNumber=9'], ['--config.buildVersion=9'], ['-c.buildNumber', '9'], ['-c.buildVersion', '9'], ['--config.buildNumber', '9'], ['--config.buildVersion', '9'], ['--config', 'custom.yml'] ]) {
      fs.rmSync(receipt, { force: true })
      const rejected = spawnSync(process.execPath, [runner, ...override], { env, encoding: 'utf8' })
      assert.notEqual(rejected.status, 0, override.join(' '))
      assert.equal(fs.existsSync(receipt), false)
    }
    for (const version of ['2.0.2', '2.0.2-beta.1']) {
      fs.writeFileSync(path.join(temp, 'package.json'), JSON.stringify({ version }))
      const unchanged = spawnSync(process.execPath, [runner, '--config', 'custom.yml'], { env: { ...env, BUILD_NUMBER: '9' }, encoding: 'utf8' })
      assert.equal(unchanged.status, 0, unchanged.stderr)
      const child = JSON.parse(fs.readFileSync(receipt, 'utf8'))
      assert.equal(child.buildNumber, '9')
      assert.ok(!child.args.some(arg => arg.startsWith('-c.buildVersion=')))
    }
  } finally { fs.rmSync(temp, { recursive: true, force: true }) }
})

test('maps four-part versions to exact native build numbers', () => {
  for (const n of [3, 4, 10]) assert.deepEqual(nativeVersionOptions(`2.0.2.${n}`), { buildNumber: `${n}`, buildVersion: `2.0.2.${n}` })
  for (const v of ['2.0.2', '2.0.2-beta.1']) assert.deepEqual(nativeVersionOptions(v), {})
})
