# Fixture build handoff — PARTIAL; independent review required

## Delivered

A real disposable local non-admin **prerequisite**, replacing synthetic token
creation. `New-LocalUser` receives a fresh OS-RNG runtime password, masked before
credential use. The account joins Users, is explicitly absent from Administrators,
and differs from the runner SID. OS `CreateProfile` plus .NET credentialed
`ProcessStartInfo` with `LoadUserProfile=true` replaces the custom process/token
launcher. Only read-only native token observation remains in the historically
named `restricted_token.py`.

The child must establish OS SID/profile/special-folder identity, WindowsPrincipal
and IsUserAnAdmin false, medium/non-elevated/limited privileges, admin-only ACL
read denial, private Clover-home writes and private Startup-directory readiness.
It receives an explicit clean environment and no CI/provider credentials.
Cleanup continues across failures and removes only the created SID's processes,
tasks, profile and account, with removal read-back. Process termination retains a
native process handle and rechecks SID before killing; it never kills by name.

The prerequisite is hosted-Windows-only and runs no installer, gateway or update.
The narrow workflow has its own explicit post-review push branch. The public
Startup registration worker still invokes actual `clover gateway install` and
supplies actual `n` to UAC; it now requires the created-account SID explicitly.
No private gateway-generation helper or fake gateway was introduced.

## Verified locally

- Exact old helper `b7b95464f9f6020adae57bee8f3fff4c0eb5c5a8` rejects the new
  genuine-account predicate: `.review/baseline-red.log` (expected RED).
- Canonical fixture suite: **44 passed, zero skips/failures**.
- Canonical frozen product suite: **85 passed, zero skips/failures**, exact SHA
  `1a606496182ef48eafd78889eea8878165b829ae`, tree
  `43825538944a3d836e8a28ad509f39b0eca7710e`.
- All changed Python compiles; Ruff passes; source-only `git diff --check` passes.
  Raw expected-RED pytest logs/XML retain their original trailing whitespace;
  they are evidence, not normalized source. PowerShell 7.4.13 parses all fixture
  scripts on Linux. PowerShell tests execute the actual environment, result,
  launcher-observation and finally-cleanup blocks with OS cmdlet stubs, including
  wrong/missing identity, admin/profile/ACL/readiness errors, failed logon,
  timeout, absent receipt, nonzero child exit, exception redaction, foreign task
  exclusion and profile-removal failure without skipping account removal.
- `.review/canonical/verified.json` counts **individual per-file JUnit XMLs**.
  The first aggregate invocation had six PowerShell skips because the canonical
  runner strips `TOKEN_TEST_PWSH`; that diagnostic remains in
  `.review/fixture-focused.log`. The final invocation supplies `pwsh` through
  the allowed PATH and executes all six tests. No skips count as proof.
- Product source, released installer bytes/checks, pins, full matrix workflow,
  old Task fixture, native controls and update acceptance driver remain unchanged.

Reproduce local checks from this worktree:

    .venv/bin/python -I .review/run_local_checks.py

The private venv, Linux PowerShell package, source clone, caches and scratch live
under the ignored `.venv/` in this isolated worktree. No shared/live venv was
installed into or modified. This is portable proof, **not native Windows proof**.

## Why this is partial — matrix integration stopped

The requested public Scheduled Task path and the unchanged matrix's live Schedule
ancestry assertion conflict at the source seam. Frozen candidate
`clover_cli/gateway_windows.py:644` uses `wscript.exe`; its generated launcher at
line 517 calls `sh.Run ..., 0, False` (detached). The current matrix at
`windows_update.py:486-487` demands the Schedule service PID in the gateway's
**live** ancestor chain. Historical `start_troy.ps1` creates a direct pythonw task
instead. It cannot be represented as the supported public task path.

I stopped and reverted the proposed matrix/Task/workflow integration rather than
weakening that assertion, patching product behavior, or claiming the synthetic
Task was equivalent. The unapproved, **not-applied** draft diff is saved only for
review at `.review/not-applied-matrix-integration.diff` (do not apply as-is).

Remaining work owned by the parent: establish/review a genuine public-Task origin
proof compatible with the existing Schedule identity fences, then integrate the
real-account lifetime across the complete Startup update driver. As currently
committed, `register_startup.ps1` fails immediately if the old driver omits the
explicit fresh-account SID. **Do not run the full matrix from this partial build.**
No generic account reapproval is needed; disposable account/profile permission is
already granted. Do not silently waive or remove the ancestry assertion.

## Narrow native gate reference — NOT EXECUTED

After independent source review and the parent's explicit one-run authorization,
only the prerequisite workflow is suitable for a bounded native check:

- Workflow: `.github/workflows/windows-token-prerequisite-e8663039.yml`
- Dedicated trigger branch: `proof/windows-real-account-prerequisite`
- Job: `account-prerequisite`, hosted `windows-2025`, five-minute limit.
- Entry: `token_prerequisite.ps1 -Python <one absolute machine Python>
  -Evidence <new runner-temp directory>`.
- Expected artifacts: `account.json`, `admin-only.sddl`, `child/token.json`,
  `child/access.json`, `RESULT.json`; cleanup must pass. Read the actual child and
  removal receipts; launch success alone is not acceptance.

No push, native execution, label change, merge, tag, publication, release,
installer/gateway run or live-agent operation occurred in this build stage.
