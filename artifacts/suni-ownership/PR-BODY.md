## Scope
Stacked on PR #148, immutable base `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`. No edits to `gateway/status.py`, Windows workflows, or live gateways. Do not merge/tag/release from this PR without the parent's combined review and remaining native acceptance.

## Product correction
- Recognize both framework-Python argv[0] normalization seams only on macOS.
- Require this updater's **venv bin directory**, live launchd **loaded** program and exact wrapper arguments bound to the wrapper PID, real/effective/saved UID, wrapper->worker edge, expected command tail, error log, and repeated process start fingerprints.
- Shared base Python inodes and claimed VIRTUAL_ENV alone never establish ownership. An unreadable or conflicting observation fails closed. Windows/Linux mismatch policy remains rejection.

## Tests and evidence
- Original immutable PR148 replay: two positive-contract failures (12 cases: 8 pass, 2 fail, 2 historical-policy skips).
- Expanded RED also catches a shared-base-interpreter false admission (3 failures).
- Fixed focused suite: **70 passed, 2 historical-policy skips**. No negative assertion removed.
- A new separate macOS workflow bootstraps on this dedicated branch's workflow-file push; no existing workflow pins changed. It records native published RED and exact-source GREEN using a Homebrew framework interpreter, actual launchd PID/argv/start evidence, strict countercases, and natural completion/owned-label cleanup.
- Separate job attempts an actual frozen 1.5.6 CLI updater against a normally installed launchd gateway and verifies source/import identity, receipt and 90-second health. It does **not** pretend terminal gateway-mode CLI is chat proof.

- Hosted run https://github.com/oaostarboy/clover-c1/actions/runs/40050040331 executed exact source `9195c15e6d40410ada6fd23b125e1e6137ad6547`.
- Framework ownership job **PASS**: published native RED and repaired GREEN, both real argv aliases reproduced, expected loaded launchd PID/program, stable starts, stale/wrong-home/unadmitted negatives rejected, natural process completion and exact-label absence verified.
- Frozen updater job **FAIL BEFORE UPDATER**: old source/import identity and normal launchd startup succeeded, but reused foreground verifier counted parent1981 and missed the framework-normalized worker because its argv has no checkout path. No updater, fresh receipt or post-update stability window ran. Original logs/archive are preserved; no retry.

## Explicit outstanding acceptance
The new update fixture explicitly marks **chat update**, **accepted turn + one tool effect + one complete reply**, and **pending no-pull catchup** NOT EXERCISED. A native ownership-seam pass is not complete update certification. A wrapper-aware strict native verifier and another reviewed full acceptance run remain required. Parent owns workflow/combined delta review and the `ci-reviewed` label; no label added by builder.

Handoff and exact per-case/native evidence: `artifacts/suni-ownership/REPAIR-HANDOFF.md`. The source repair is built and native-exercised; this is **not merge-ready update certification**.
