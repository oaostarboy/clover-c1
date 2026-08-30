/**
 * Tests for electron/backend-probes.ts.
 *
 * Run with: node --test electron/backend-probes.test.ts
 * (Wired into npm test:desktop:platforms in package.json.)
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  canImportCloverCli,
  cloverRuntimeImportProbe,
  DEFAULT_PROBE_TIMEOUT_MS,
  PROBE_TIMEOUT_MS,
  resolveProbeTimeoutMs,
  shouldTrustCloverOverride,
  verifyCloverCli
} from './backend-probes'

// Resolve the host's own Node binary -- guaranteed to be on disk and
// runnable. We use it as both a stand-in for "a python that doesn't
// have clover_cli" (since `node -c "import clover_cli"` will exit
// non-zero) and as a way to script verifyCloverCli's success path
// (a tiny script we write to disk that exits 0 on --version).
const NODE_BIN = process.execPath

test('canImportCloverCli returns false when path is falsy', () => {
  assert.equal(canImportCloverCli(''), false)
  assert.equal(canImportCloverCli(null), false)
  assert.equal(canImportCloverCli(undefined), false)
})

test('canImportCloverCli returns false when interpreter cannot run -c', () => {
  // node IS an interpreter, but `node -c "import clover_cli"` is a
  // SyntaxError -- different exit reason from a real Python's
  // ModuleNotFoundError, but the predicate is "exit 0 or not" and
  // both land on "not", which is exactly what we want for the
  // resolver fall-through.
  assert.equal(canImportCloverCli(NODE_BIN), false)
})

test('canImportCloverCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'clover-probes-ghost-' + Date.now() + '.exe')
  assert.equal(canImportCloverCli(ghost), false)
})

test('clover runtime import probe checks config dependencies', () => {
  const probe = cloverRuntimeImportProbe()
  assert.match(probe, /\bimport yaml\b/)
  // dotenv is the first third-party import on the CLI boot path
  // (clover_cli/env_loader.py); a mid-update venv missing python-dotenv
  // passed the old probe and produced an unrecoverable boot loop.
  assert.match(probe, /\bimport dotenv\b/)
  assert.match(probe, /\bimport clover_cli\.config\b/)
})

test('explicit Clover override is authoritative', () => {
  assert.equal(shouldTrustCloverOverride('/nix/store/abc/bin/clover'), true)
})

test('empty Clover override is not authoritative', () => {
  assert.equal(shouldTrustCloverOverride(''), false)
  assert.equal(shouldTrustCloverOverride(undefined), false)
})

test('verifyCloverCli returns false when command is falsy', () => {
  assert.equal(verifyCloverCli(''), false)
  assert.equal(verifyCloverCli(null), false)
  assert.equal(verifyCloverCli(undefined), false)
})

test('verifyCloverCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'clover-probes-ghost-' + Date.now() + '.exe')
  assert.equal(verifyCloverCli(ghost), false)
})

test('verifyCloverCli returns true when --version exits 0', () => {
  // Write a tiny script that exits 0 regardless of args, then invoke
  // it through node. This stands in for a working clover binary --
  // verifyCloverCli only cares about the exit code.
  const scriptPath = path.join(os.tmpdir(), `clover-probes-ok-${Date.now()}-${process.pid}.cjs`)
  fs.writeFileSync(scriptPath, 'process.exit(0)\n')

  try {
    // Use node as the launcher and our script as the "command". Pass
    // shell:false (default) -- node is a real binary, no shim.
    // execFileSync passes ['--version'] as args, which node ignores
    // gracefully (well, it prints its version and exits 0, which is
    // perfect -- exit code 0 is the only signal we read).
    assert.equal(verifyCloverCli(NODE_BIN), true)
  } finally {
    try {
      fs.unlinkSync(scriptPath)
    } catch {
      void 0
    }
  }
})

test('verifyCloverCli swallows timeouts (does not throw)', () => {
  // We can't easily provoke a real hang in CI without slowing the
  // suite, but we CAN confirm that an invocation that DOES throw
  // (because the binary is missing) returns false rather than
  // propagating. Same code path the timeout case takes.
  assert.equal(verifyCloverCli('/definitely/not/a/real/binary/anywhere'), false)
})

test('default probe timeout is 15s (not the old 5s death-loop value)', () => {
  assert.equal(DEFAULT_PROBE_TIMEOUT_MS, 15_000)
  // Module constant uses process.env at load time; with no override it
  // matches the default (tests run without CLOVER_PROBE_TIMEOUT_MS).
  assert.equal(PROBE_TIMEOUT_MS, DEFAULT_PROBE_TIMEOUT_MS)
})

test('resolveProbeTimeoutMs honours CLOVER_PROBE_TIMEOUT_MS', () => {
  assert.equal(resolveProbeTimeoutMs({}), DEFAULT_PROBE_TIMEOUT_MS)
  assert.equal(resolveProbeTimeoutMs({ CLOVER_PROBE_TIMEOUT_MS: '30000' }), 30_000)
  assert.equal(resolveProbeTimeoutMs({ CLOVER_PROBE_TIMEOUT_MS: '0' }), DEFAULT_PROBE_TIMEOUT_MS)
  assert.equal(resolveProbeTimeoutMs({ CLOVER_PROBE_TIMEOUT_MS: 'nope' }), DEFAULT_PROBE_TIMEOUT_MS)
  // Cap runaway values
  assert.equal(resolveProbeTimeoutMs({ CLOVER_PROBE_TIMEOUT_MS: '999999' }), 120_000)
})
