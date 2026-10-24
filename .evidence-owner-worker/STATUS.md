# Windows updater proof checkpoint

## Ownership and next action

Test-only owner: subagent-sa-0-af0ff5ad, branch `clover-subagent/subagent-sa-0-af0ff5ad`.
Draft proof PR: https://github.com/oaostarboy/clover-c1/pull/149 (no ci-reviewed label).
Product PR148 currently ends at27f507. Parent owns the immutable COMBINED source
handoff, fixture review and successor execution. No final combined update gate
has started. Do not tell users that the Windows update is certified yet.

The staged successor is committed locally but intentionally not pushed with the
old native-safety-only pins: that would repeat a narrow run instead of exercising
the missing combined gate. Last hosted fixture is6ac34feebea34aa35ad1a9182788d25d48ad46c6.
Parent should review the local test-only delta, provide the exact combined SHA/tree
including27f507 and the separately owned Mac guard repair, set pins mode=candidate,
scope=full, target/target_tree, then push this proof branch ONCE. Its push workflow
runs native safety plus all16 updates and ordinary repository CI. Do not replace
historical refs, inject frozen-caller capabilities, retry a refusal or restart
from the driver. Collect canonical artifacts/logs and require every row from that
single invocation; do not aggregate old passes into acceptance.

Collector command after terminal run read-back (replace uppercase placeholders):
`python .evidence-owner-worker/collect.py --final --run RUN_ID --workflow-sha PROOF_HEAD_SHA --product-sha COMBINED_SHA --scope full`.
Ordinary PR CI and parent review remain separate from that native collector.

## Verified evidence

- Baseline40049065987, exact released1.5.9:70 cases,64 passed,4 failed,2 skipped.
  Real reporters overwrote the owner identity; separate native clock and two
  inappropriate POSIX selections also failed. All original evidence survives.
- Native safety40049337485 onbdad7a7b6b6698f347e06fd305a2bc6c3dc9055e:
  56 passed/2 POSIX-only skips. Its16 updater rows never launched gateways:
  the unchanged SQLite prerequisite rejected the initial fixture's catalog.
- Corrected full diagnostic40049740582 on that same old source:8 Startup cases
  passed,8 Troy cases failed the observer, whole invocation FAILED.
- Explicit partial Troy correction40050690083:8 Task cases passed. The ledger
  independently checks all16 unique diagnostic cases across those two fixtures:
  exact sources, fresh success receipts, one effect/reply, one gateway/listener
  stable for at least90s, saved state/customization and chat completion notice.
  This is NOT one green full gate and does not certify newer source.
- Native security40052775499, product27f507acb4f12c30d237471095f137007952c312,
  tree56ef6f6990bdb3d0d56fca31f582980857dca15e, fixture6ac34fee:
  **63 passed,2 POSIX-only skips**. All7 new Windows fdopen r+, new/existing file,
  real byte-range contention/release/process-exit and hardlink/reparse/directory
  refusal cases passed. The updater matrix was deliberately SKIPPED.
  build_ledger.py rechecks canonical JUnit, source-sha, raw ZIP SHA256 and CRC.

## Staged successor, not yet native-executed

-16 unsafe-status post-pull seams:missing/invalid JSON/reused birth/foreign home
  crossed with exact1.5.6-Suni,1.5.8,1.5.9 and candidate callers. Original import,
  managed-uv call and outer exception handlers come directly from immutable Git
  blobs. The NEW library must refuse with SystemExit before dependency work;
  actual native children, status bytes and private pending state must survive.
  These are excerpt-seam controls, not full negative updater runs or pre-pull
  migration. Whole positive old-caller updates are separate matrix rows.
-Explicit setup-uv uv-path input, absolute existing-file validation, binary hash
  and0.13.5 version evidence; the safe-SQLite prerequisite remains unchanged.
-Native SCM inventory/unchanged foreign Schedule PID assertions in update rows.

Local checks:11 portable contracts passed; all7 managed-runtime-policy tests
passed after reproducing the bare-uv failure; compile/diff checks passed.
Scoped Ruff0.15.10 passed. Hosted Ruff0.17.2 could not be installed locally
(index reported no such version); its five reported fixture encoding sites are
corrected with explicit UTF-8, but actual hosted recheck remains pending.
Standard CI40052777927 remains historically RED:ruff plus the bare-uv policy test.
No product exemption, linter rule, credential, ACL or permission setting changed.

## Evidence layout and limits

DIAGNOSTIC-LEDGER.json and40052775499/NATIVE-SECURITY-RESULT.json are shareable
non-secret summaries. Complete native artifacts and raw ZIPs remain here on the
local worktree; .gitignore keeps bulky/private fixture evidence out of commits.
The archived frozen watcher retains its original bytes as .py.txt, not executable
source. No proof claims cross-user Windows ACL isolation, live Telegram/phone
transport, busy protection of a frozen Windows first stop, large-state rollback,
Linux1.5.8 frozen pre-pull recovery, publication or actual user upgrades.
