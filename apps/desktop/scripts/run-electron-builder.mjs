// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"
import { nativeVersionOptions } from "./native-version-options.mjs"

const require = createRequire(import.meta.url)

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

const dist = electronDistDir()
const args = []
const packageData = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const { version } = packageData
const native = nativeVersionOptions(version)
if (native.buildNumber !== undefined) {
  for (const key of ['buildNumber', 'buildVersion']) {
    const value = packageData.build?.[key]
    if (value != null && value !== native[key]) {
      console.error(`[run-electron-builder] ${key} in package build config conflicts with package version ${version}`)
      process.exit(1)
    }
  }
  if (process.env.BUILD_NUMBER && process.env.BUILD_NUMBER !== native.buildNumber) {
    console.error(`[run-electron-builder] BUILD_NUMBER conflicts with package version ${version}`)
    process.exit(1)
  }
  const cliArgs = process.argv.slice(2)
  if (cliArgs.some(arg => /^(?:-c|--config)(?:$|=)/.test(arg) || /^(?:-c|--config)\.(?:buildNumber|buildVersion)(?:=|$)/.test(arg))) {
    console.error('[run-electron-builder] custom config or buildNumber/buildVersion CLI overrides are not supported for four-part versions')
    process.exit(1)
  }
  args.push(`-c.buildVersion=${native.buildVersion}`)
}
if (dist && fs.existsSync(distBinary(dist))) {
  args.push(`-c.electronDist=${dist}`)
} else {
  console.warn(
    "[run-electron-builder] no local electron dist; electron-builder will fetch " +
      "via @electron/get (electronVersion + ELECTRON_MIRROR)."
  )
}
args.push(...process.argv.slice(2))

const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
  stdio: "inherit",
  env: native.buildNumber === undefined ? process.env : { ...process.env, BUILD_NUMBER: native.buildNumber },
})
if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
