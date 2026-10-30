import fs from 'node:fs'
import path from 'node:path'

const desktopRoot = path.resolve(import.meta.dirname, '..')
const repoRoot = path.resolve(desktopRoot, '..', '..')

// Cross-platform: allow 4-part versions (e.g. 2.0.2.2) in electron-builder normalizePackageData
const normalizePaths = [
  path.join(repoRoot, 'node_modules', 'app-builder-lib', 'out', 'util', 'normalizePackageData.js'),
  path.join(desktopRoot, 'node_modules', 'app-builder-lib', 'out', 'util', 'normalizePackageData.js')
]
for (const normPath of normalizePaths) {
  if (fs.existsSync(normPath)) {
    const src = fs.readFileSync(normPath, 'utf8')
    if (!src.includes('semver.coerce')) {
      const patched = src.replace(
        'if (!semver.valid(data.version, loose)) {',
        'const coerced = semver.coerce(data.version); if (coerced) { data.version = coerced.version; return true; } if (!semver.valid(data.version, loose)) {'
      )
      if (patched !== src) {
        fs.writeFileSync(normPath, patched, 'utf8')
        console.log('[patch-electron-builder] applied 4-part version fallback to normalizePackageData.js')
      }
    }
  }
}

if (process.platform !== 'darwin') {
  process.exit(0)
}

const electronMacPath = path.join(repoRoot, 'node_modules', 'app-builder-lib', 'out', 'electron', 'electronMac.js')

const marker = 'clover-macos-electron-binary-fallback'
const needle = `    await Promise.all([
        doRename(path.join(contentsPath, "MacOS"), electronBranding.productName, appPlist.CFBundleExecutable),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSE")),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSES.chromium.html")),
    ]);`
const replacement = `    // ${marker}: electron-builder 26.8.x can sometimes copy
    // Electron.app without its main MacOS/Electron binary before this rename.
    // Restore it from the installed Electron runtime so local desktop installs
    // do not fail with ENOENT during macOS arm64 packaging.
    const macosDir = path.join(contentsPath, "MacOS");
    const bundledElectronBinary = path.join(macosDir, electronBranding.productName);
    if (!fs.existsSync(bundledElectronBinary)) {
        const candidates = [
            path.join(packager.info.framework.distMacOsAppName, "Contents", "MacOS", electronBranding.productName),
            // npm may nest the workspace-only electron devDep under
            // apps/desktop/node_modules (process.cwd() during pack), or hoist
            // it to the repo root. Try the workspace-local install first, then
            // the root hoist, so the fallback works under either layout.
            path.join(process.cwd(), "node_modules", "electron", "dist", "Electron.app", "Contents", "MacOS", electronBranding.productName),
            path.join(process.cwd(), "..", "..", "node_modules", "electron", "dist", "Electron.app", "Contents", "MacOS", electronBranding.productName),
        ];
        const sourceBinary = candidates.find(candidate => fs.existsSync(candidate));
        if (sourceBinary == null) {
            throw new Error("Electron binary missing from packaged app and Electron runtime: " + bundledElectronBinary);
        }
        await (0, promises_1.copyFile)(sourceBinary, bundledElectronBinary);
        await (0, promises_1.chmod)(bundledElectronBinary, 0o755);
    }
    await Promise.all([
        doRename(macosDir, electronBranding.productName, appPlist.CFBundleExecutable),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSE")),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSES.chromium.html")),
    ]);`

if (!fs.existsSync(electronMacPath)) {
  console.warn(`[patch-electron-builder] skipped: ${electronMacPath} not found`)
  process.exit(0)
}

const source = fs.readFileSync(electronMacPath, 'utf8')
if (source.includes(marker)) {
  console.log('[patch-electron-builder] macOS Electron binary fallback already applied')
  process.exit(0)
}

if (!source.includes(needle)) {
  console.warn('[patch-electron-builder] skipped: expected electronMac.js shape not found')
  process.exit(0)
}

fs.writeFileSync(electronMacPath, source.replace(needle, replacement))
console.log('[patch-electron-builder] applied macOS Electron binary fallback')
