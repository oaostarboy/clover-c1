// Read-only bounded probes; never modify trust, installer or download cache.
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
const output = process.argv[2];
const version = '41.10.5';
const results = { node: process.version, arch: process.arch, platform: process.platform,
  version, tlsVerificationDisabled: process.env.NODE_TLS_REJECT_UNAUTHORIZED === '0',
  probes: [] };
if (results.tlsVerificationDisabled) throw new Error('TLS verification is disabled');
for (const name of [`electron-v${version}-darwin-arm64.zip`, 'SHASUMS256.txt']) {
  const url = `https://github.com/electron/electron/releases/download/v${version}/${name}`;
  const start = Date.now();
  try {
    const response = await fetch(url, { method: 'HEAD', signal: AbortSignal.timeout(30000) });
    results.probes.push({ artifact: name, status: response.status,
      finalHost: new URL(response.url).hostname, elapsedMs: Date.now() - start });
    await response.body?.cancel();
  } catch (error) {
    // Closed allowlist: arbitrary URL, certificate or header text is not logged.
    const causes = []; let current = error;
    for (let i = 0; current && i < 5; i++, current = current.cause)
      causes.push({ name: current.name, code: current.code, syscall: current.syscall });
    results.probes.push({ artifact: name, causes, elapsedMs: Date.now() - start });
  }
}
fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(results, null, 2));
console.log(JSON.stringify(results));
