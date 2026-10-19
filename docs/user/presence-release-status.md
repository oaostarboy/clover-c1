# Presence candidate verification — preparation snapshot

This is an **unpublished preparation snapshot**, not a release approval or a
recommendation to update. Presence remains disabled by default. The candidate
still reports version 1.5.6; 1.5.7 has not been cut.

Product candidate: `a5bd8d80e292747fa52435ca20e311f1ca91dcd1`, tree
`ec5219e6536730b823fa4366e0285ed51cc2a11b`. The source was independently reviewed
previously; the native fixes add corrupt-JSON recursion handling and disconnect
the evaluator's metadata probe from terminal stdin. Documentation-only additions
above that product tree do not imply another native execution.

## Platform evidence

[Exact-product Windows/macOS run 39188162300](https://github.com/oaostarboy/clover-c1/actions/runs/39188162300)
checks out the product candidate separately from its helper-workflow commit
`db9f18baffe190ee7ad5cd249341450e53fd219c`. Inspect the jobs and artifacts, not
only the run badge.

- macOS joined integration plus stdin scanner: **93 files, 2,219 passed, zero
  failures, errors, skips or retries**, including both mounted-renderer tests.
- At this preparation readback, macOS full-suite shard 2/4 had **failed**;
  its final test accounting is still owned by the native worker. Windows joined
  and the seven other full-suite shards were still active. Those results are
  **pending**, not passed. A passing joined row does not clear the full gate.
- Installed Electron, native fresh installation, running-gateway update and
  rollback have **not been exercised** by these source-suite jobs.
- Original macOS whole-suite failure evidence remains preserved in
  [run 39186545631](https://github.com/oaostarboy/clover-c1/actions/runs/39186545631).
  Actual primary JUnit elements recorded 48,433 passes, 71 failures, 5 errors and
  527 skips. The canonical summary reports call-phase totals differently;
  teardown errors must not be omitted.
- The original Linux full suite retained 126 baseline-shared failures plus a new
  evaluator stdin failure. The latter has a preserved failing scanner and a
  passing narrow correction. The 126 shared failures remain **unwaived**.
- A separate provisional Linux staging environment passed **136 tests across six
  files**, zero failures/skips/retries, using the canonical runner with private
  network/filesystem boundaries. Dependency metadata matched the 129-package
  existing environment; dependency checking and neutral-directory editable
  import checks passed. This is not a full-suite or native certificate.
- Prior Linux Electron execution retained two missing-native-Electron suite-load
  errors. A JavaScript build is not installed Electron acceptance.

## Synthetic model smoke: blocked, no result

The global authorization is three physical synthetic requests **total**, not per
worker or retry. The two existing ledgers recorded zero attempted, completed or
uncertain requests and none in flight. The read-only native-owner preflight held
with `selected_authority_missing` before model entry: the separate test process
had no existing task-local selected connection owner. No account selection,
refresh, copied credential, fallback, retry, budget change or live enablement may
be used to bypass that guard. No real-model quality or accuracy claim is made.

## Publication and next-start status

This snapshot has no merged presence PR, public 1.5.7 tag or release. A candidate
branch pushed for CI is not main publication. A dedicated provisional dependency
environment is prepared, but it is **not selected for next start**. The existing
gateway remains unchanged. Native ownership and the request ledger must transfer
explicitly before a successor can consume any remaining smoke allowance.

Required next steps: terminal native receipts and repairs, authorized smoke or a
truthful unresolved guard, final exact-source review/required CI and affected
install/update/rollback proof, reviewed publication/readback, then verified
reversible next-start selection and worker recovery checks. Do not treat this
file as permission to restart or activate presence.
