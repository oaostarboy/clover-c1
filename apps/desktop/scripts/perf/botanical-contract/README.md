# Botanical raster contract fixture

From a checkout with dependencies installed, prepare a new output directory:

```
node apps/desktop/scripts/perf/botanical-contract/prepare.mjs BASELINE_COMMIT /tmp/botanical-contract-output
```

`BASELINE_COMMIT` is resolved using git; the fixture bundles its original raster and the current working-tree candidate. `provenance.json` records the actual baseline and HEAD IDs, input SHA-256 hashes, and fixture hashes. A dirty tree is allowed and must be identified through the recorded hashes, not inferred from HEAD. Transfer the generated output directory to a machine with the desired Electron version and run its `main.cjs` with that Electron executable. It uses a separate `user-data` inside the output directory, creates a hidden offscreen window, and writes `contract-result.json`. Assert 44 cases with zero `different` bytes and 12 cache cycles. Review the result rather than relying solely on process exit code. Do not place the output in a real Clover home.

This hidden offscreen fixture does not verify a full app, displayed motion, or native glass. For the 1,800-frame cache sample, also run `plateau-main.cjs` in a fresh output directory with the same Electron executable and compare snapshots in `plateau-result.json`: the measured fields remain unchanged from frames 960 through 1800 for the study workload. This is not an arbitrary-coordinate or garbage-collection proof. The original frame-equivalence and pacing experiments require separate fixtures; this script does not claim to reproduce them.
