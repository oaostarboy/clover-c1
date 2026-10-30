// Prepare a portable native Electron byte-comparison fixture without touching dist.
// Usage: node prepare.mjs BASELINE_COMMIT OUTPUT_DIRECTORY
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '../../../../..')
const [baseline, destination] = process.argv.slice(2)
if (!baseline || !destination) throw new Error('Usage: node prepare.mjs BASELINE_COMMIT NEW_OUTPUT_DIRECTORY')
const out = resolve(destination)
mkdirSync(out, { recursive: false })
const source = 'apps/desktop/src/app/chat/botanical-raster.ts'
const baselineSha = execFileSync('git', ['rev-parse', `${baseline}^{commit}`], { cwd: repo, encoding: 'utf8' }).trim()
const original = execFileSync('git', ['show', `${baselineSha}:${source}`], { cwd: repo })
writeFileSync(resolve(out, 'baseline-raster.ts'), original)
writeFileSync(resolve(out, 'original-entry.ts'), "export { botanicalRaster } from './baseline-raster'\n")
const chat = resolve(repo, 'apps/desktop/src/app/chat')
const entry = `export { botanicalRaster } from ${JSON.stringify(resolve(chat, 'botanical-raster.ts'))}\nexport { drawBotanical } from ${JSON.stringify(resolve(chat, 'botanical-surface.ts'))}\n`
writeFileSync(resolve(out, 'candidate-entry.ts'), entry)
for (const name of ['original', 'candidate']) {
  await build({ entryPoints: [resolve(out, `${name}-entry.ts`)], outfile: resolve(out, `${name}.mjs`), bundle: true, format: 'esm', platform: 'browser', logLevel: 'silent' })
}
for (const name of ['main.cjs', 'contract.html', 'plateau-main.cjs', 'plateau.html']) copyFileSync(resolve(here, name), resolve(out, name))
const hash = p => createHash('sha256').update(readFileSync(p)).digest('hex')
const inputs = [source, 'apps/desktop/src/app/chat/botanical-surface.ts']
const provenance = { baseline: baselineSha, head: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim(), candidateFiles: Object.fromEntries(inputs.map(p => [p, hash(resolve(repo, p))])), baselineRasterSha256: createHash('sha256').update(original).digest('hex'), fixtures: Object.fromEntries(['main.cjs', 'contract.html', 'plateau-main.cjs', 'plateau.html', 'original.mjs', 'candidate.mjs'].map(p => [p, hash(resolve(out, p))])) }
writeFileSync(resolve(out, 'provenance.json'), JSON.stringify(provenance, null, 2) + '\n')
console.log(JSON.stringify({ output: out, provenance }, null, 2))
