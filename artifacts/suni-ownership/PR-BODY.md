## Scope and source pins
Stacked on PR #148. Initial immutable base was `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`; this branch now includes reviewed status-lock correction `27f507acb4f12c30d237471095f137007952c312` unchanged. No independently authored status edits, Windows workflow edits, existing workflow pin changes or live gateway operations.

**Final code / Windows retarget:** `bd8b623942d75e6f786ca9f4ef5267c454f9624f`. **Native-executed combined source:** `6ac82c5cb5e56702f6616245140fd5efb227805c` (tree `202b32c8abfddb187ed45e9e662e186b558c5c0d`). The sole later code change is a Windows skip for the POSIX-path Mac replay; product and native fixture/workflow files are byte-identical. That decorator is a no-op on macOS; no native run of bd8b623 is claimed. Final local218/4 interactions passed at bd8b623. Later evidence-only commits do not change code.

## Product correction
- Recognize both framework-Python argv[0] normalization seams only on macOS.
- Require this updater's **venv bin directory**, live launchd **loaded** program and exact wrapper arguments bound to the wrapper PID, real/effective/saved UID, direct wrapper->worker edge, expected command tail/profile error log, and repeated start fingerprints.
- Shared base Python inodes and claimed VIRTUAL_ENV alone never establish ownership. Unreadable or conflicting evidence fails closed. Windows/Linux mismatch policy remains rejection.

## Verified results
- PR148 original replay: two exact positive-contract failures. Expanded RED additionally catches a shared-base false admission in the old lexical-only path. No negative assertion was removed.
- Combined local status/guard/verifier interactions: **218 passed, 4 skips** (two pinned historical-policy cases; two native Windows-only cases). Canonical JUnit and every unique node ID are committed.
- Exact combined hosted macOS run **PASS, both jobs**: https://github.com/oaostarboy/clover-c1/actions/runs/40050938504 . Homebrew framework Python, actual Darwin/launchd, private credential-free installation, no production credentials.
- Native ownership: published RED and repaired GREEN, both argv aliases reproduced; stable PID/start identities; stale/wrong-home/unadmitted negatives rejected; synthetic processes finished naturally and exact labels were verified absent.
- Actual frozen 1.5.6 CLI updater: normal supported launchd installation, running old gateway -> exact combined target, updater exit0, fresh success receipt, new worker9164/wrapper9150, owned listener and90-second stable window, no stale restart_requested, no failed units or killed_pids. Installed source/import identity passed before/after. Private service cleanup verified label absence.
- First full-native attempt failed before the updater because the old foreground verifier counted the wrapper and missed normalized worker argv. Its complete failure evidence remains committed. A new **Mac-only** verifier now requires exactly the worker plus its positively proved wrapper; no ancestor blanket exemption or shared-verifier relaxation.

## Still not proved / integration hold
Chat `/update`, one accepted turn with one tool effect and one complete user reply, and staged pending no-pull catchup remain **NOT EXERCISED**. This is real frozen CLI proof, not full chat/update certification or Suni recovery. Parent owns combined-delta/workflow review, `ci-reviewed`, remaining acceptance and Windows retargeting. No merge, tag, release, live restart or approval label was performed by the builder.

Handoff, exact receipts/source hashes/PID starts, raw archived logs and original RED evidence: `artifacts/suni-ownership/REPAIR-HANDOFF.md`.
