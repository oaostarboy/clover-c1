# macOS framework wrapper repair — native evidence checkpoint

## Ownership and scope

- Sole product file: `clover_cli/update_delivery_guard.py`.
- No `gateway/status.py`, Windows workflow, existing workflow pin, live source, home, database, venv or service was edited.
- Branch `clover-subagent/subagent-sa-0-578b1794`; stacked PR https://github.com/oaostarboy/clover-c1/pull/150.
- Immutable PR148 base: `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`.
- Inherited tests/evidence cherry-picked as `83306449` from `ed4c7efb33685ede48390a0c6596e804d8f8d0b4`.
- Product/workflow tested source: `9195c15e6d40410ada6fd23b125e1e6137ad6547`, tree `acbd3f9558424190acb7054e74e43d385aa5392f`.
- Guard SHA256: `f16befb42735a4336a3ee4e13440d56f8574be24d9787fce5726846050baacd0`.

## Security boundary

Interpreter normalization is only admitted on macOS, with all of these proofs:
this updater's venv **bin directory** (not executable realpath), its pyvenv marker,
the native framework executable, a loaded launchd job's exact venv program and
wrapper argument vector bound to that wrapper PID, real/effective/saved UID,
launchd->wrapper->admitted worker direct edges, exact worker command tail,
expected profile error log, and repeated child/wrapper start identities.
Readable environment fields must not conflict with the independently proved venv
and home. Environment alone, a shared base interpreter, a plist on disk or an
ancestor relationship is never authority. Unknown/unreadable evidence rejects.
Windows and Linux mismatches remain rejected; lexically equal non-framework
controls retain existing policy. No carrier capability is manufactured.

## RED / GREEN

- Original PR148 replay: 12 cases; 8 pass, 2 exact positive failures, 2 historical-policy skips. `repair-red.log` preserves this run.
- Expanded final PR148 replay: 27 cases; 22 pass, 3 fail, 2 skips. The added third RED is an actual shared-base-command false admission in the old lexical-only path. `repair-final-red.log` preserves it.
- Fixed focused regression suite: 72 unique JUnit cases; **70 pass, 0 fail, 0 error, 2 historical-policy skips**. `local-test-nodeids.json` enumerates every node. `repair-junit.xml` is the canonical count source.
- Native-unit replay uses mocked OS launchd/file attestation in addition to the original process replay. Its positive expectations were not weakened. Actual native process proof below uses real psutil and launchctl, not these mocks.
- Linux real-process fixture passed, explicitly labelled NOT MACOS PROOF.
- YAML loads, all five workflow shell blocks pass `bash -n`, and `git diff --check` passed.

## Real hosted macOS proof

One bounded branch-push workflow run, no retries:
https://github.com/oaostarboy/clover-c1/actions/runs/40050040331

Framework ownership job: **PASS**
https://github.com/oaostarboy/clover-c1/actions/runs/40050040331/job/120740479414

- Homebrew framework Python 3.12.14, actual hosted Darwin/launchd.
- Published native RED `3b54f413376eb99f8b83bfd4a0a340d01825014d`: wrapper3071 -> worker3072; both OS argv[0] framework aliases; guard returns false at original line40; source hash `d16cfaa9a118cdc477134d8cb36db906d5753cdc4dc2e263d07ac4ef6b2cc509`.
- Exact repaired native GREEN `9195c15e6d40410ada6fd23b125e1e6137ad6547`: wrapper3089 -> worker3090; both framework aliases reproduced; loaded launchd program identifies the private venv; owner admitted.
- GREEN wrapper start179280544285, worker start179280544301, stable during proof; stale-worker, wrong-home and unadmitted-worker cases rejected.
- Both private synthetic workers/wrappers finished naturally and both exact fixture labels were removed and verified absent. No forced kill or replay.
- Raw job log and downloaded JSON/replay artifacts are under `hosted-40050040331/`.

Frozen normal launchd CLI updater job: **FAIL — prerequisite verifier refusal, updater NOT EXERCISED**
https://github.com/oaostarboy/clover-c1/actions/runs/40050040331/job/120740754790

The old 1.5.6 private editable installation and outside-checkout import/source
identity passed. The supported `gateway install` route loaded launchd and the
actual old gateway connected its localhost API listener. However, the reused
foreground-oriented `proof.py:observe_gateway` requires the checkout path in
each candidate's argv. In the native framework shape the worker argv contains
only the framework executable and module tail; only its wrapper carries the
venv/checkout path. The verifier therefore repeatedly reported
`gateway process count/ownership mismatch: [1981]`. The gateway's own shutdown
context identifies 1981 as its parent, confirming the counted process was the
wrapper rather than the worker. The test never ran the updater and produced no
update receipt or 90-second post-update evidence. This is a native verifier
coverage failure, NOT evidence that the product repair failed or passed update.

The owned private service stopped cleanly through the supported command; logs
show a 0.11-second graceful shutdown with no active work, and the exact launchd
label was verified absent. No retry or additional proof dispatch was made.
`hosted-update-40050040331/` retains the acceptance report, install/identity
outputs and complete gateway/verifier logs. `hosted-40050040331/logs.zip` retains
the original workflow log archive; ZIP integrity was checked successfully.

Parent must review a narrow **new macOS verifier** that counts exactly one
worker plus its positively proved wrapper (not an ancestor exemption), then
approve/dispatch the additional full native acceptance. Existing shared verifier
and Windows files remain untouched. The current workflow deliberately retains
this failed gate rather than relaxing its process count or declaring success.

## Explicit unresolved acceptance / parent ownership

- The current fixture does not exercise a genuine accepted chat turn with one
  tool effect and one complete reply, chat `/update`, or pending no-pull catchup.
  These remain **NOT EXERCISED**, never inferred from a healthy gateway.
- No full macOS update certification, merge readiness, release clearance, Suni
  recovery, idle/delivery proof or live activation is claimed.
- Parent reviews the new workflow and combined PR148+150 delta before adding
  `ci-reviewed`, coordinates the Windows owner's separate retargeting and owns
  any separately authorized recovery. Builder adds no approval label, merge,
  tag, release, rollout or additional proof dispatch.
