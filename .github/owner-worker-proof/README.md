# Windows owner/worker proof (test-only)

## Real-account prerequisite build: partial, review required

The synthetic restricted-token launcher is removed. `restricted_token.py` now
only observes actual OS identity, integrity, elevation, group membership and
privileges. Its historical filename does **not** mean it manufactures a token.
The contract is explicitly the newly created account SID, different from the
runner SID, Users membership, and no Administrators SID (not deny-only admins).

`token_prerequisite.ps1` invokes `disposable_account.ps1` only on a disposable
hosted Windows runner. The parent generates and immediately masks a random
runtime password, passes a SecureString through the standard .NET credential
process API, provisions the account's profile with OS `CreateProfile`, and sets
`LoadUserProfile=true`. No password enters argv, JSON, task XML, child environment
or artifacts. The child receives an explicit minimal environment and uses its own
OS profile, AppData and Clover home. Only this fixture's new profile/evidence
DACLs change; source/tool/global ACLs, UAC, services and policy do not change.

The real child must prove intended SID, non-admin WindowsPrincipal and
IsUserAnAdmin results, medium integrity/non-elevation/limited privileges, an
admin-only file read denial, private Clover-home writes, and writable OS-known
Startup directory. This is **registration readiness**, not an installed gateway,
installer run, update, or Task-Scheduler acceptance. Finally cleanup targets only
the created SID's processes/tasks/profile/account, reads each removal back, and
fails on cleanup error. Cleanup errors do not skip later cleanup steps.

The small prerequisite workflow retains the historical filename
`windows-token-prerequisite-e8663039.yml`. After independent review only, an
explicit push to `proof/windows-real-account-prerequisite` can run that one job.
No push or native execution occurred in this build stage.

**The full update matrix is deliberately NOT integrated with the new account
lifecycle yet.** `register_startup.ps1` accepts an explicit `ExpectedSid` and
fails closed when the old driver omits it. The public CLI and actual `n` UAC
answer remain unchanged; neither a private helper nor fabricated gateway replaces
them. The existing full driver, Task fixture, matrix/pins, safety controls and
acceptance assertions remain unchanged. Do not dispatch the full matrix from
this partial build.

Integration stopped at a source-level conflict: the real public Scheduled Task
runs a VBS whose `sh.Run ..., 0, False` detaches its gateway. The unchanged matrix
requires a live Schedule ancestor. Its historical `start_troy.ps1` instead creates
a direct pythonw task. Replacing that synthetic task while retaining every
assertion needs a separately reviewed task-origin proof contract, not silently
weakening the ancestry check. See `.review/BUILD.md` and the explicitly
**not-applied** integration diff. This is a technical parent-owner decision;
account creation/deletion permission is already granted for disposable CI.

## Scope and pins

This branch owns only the new proof directory and workflow. No product edits,
installation into the live environment, release, merge, credentials, service
permission changes, Git replacement refs, refusal retry, or driver-owned restart.

`pins.json` is the versioned parameter handoff: exact candidate SHA/tree, exact
released callers, and the historical helper SHA. A push executes those pins, not
whatever main happens to contain. Historical `mode=baseline` runs are retained
at their original workflow commits; this successor's unconditional repair-ancestry
check intentionally prevents redispatching release-only baselines. Use
`mode=native-safety` for reviewed correction-only controls, or `mode=candidate`
with an independently verified new SHA/tree for the full matrix. This new
workflow intentionally is not registered on main.
Parent review is required before adding `ci-reviewed` or merging the fixture.

**Current pins select `candidate` / `full` on the parent-authorized PR150
head `1a606496182ef48eafd78889eea8878165b829ae`, tree
`43825538944a3d836e8a28ad509f39b0eca7710e`.** The correction is unpublished and
held for bounded independent F1 delta review before the final native gate.
The prior `native-safety` invocation remains historical; it did not exercise
updates. The explicit historical eight-row `troy-verifier-correction` scope is
restricted to old bdad7 and cannot clear a future source. Require all16 rows
from the final invocation rather than adding passes from different fixtures.
The revised observer copies the proven PS probe's exact installed-image ->
direct-child -> measured-base-image relationship. It additionally records raw
process/status evidence, requires the status owner to be that worker, rejects
extra gateway processes, and retains native birth, listener and source checks.
No process environment or argv prefix can substitute for those witnesses.

- Historical diagnostic candidate: PR148, `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`.
- Candidate tree: `04630f4788743b5b556e5e4379d998c42394efa3`.
- This candidate does not contain the separately owned Suni wrapper repair.
  A changed combined product source needs a new exact-source gate.
- Reused helper checkout: `93cf13e9f66be56428993402c1e80422d9005d85`.
  `windows_update.py` derives from that revision's
  `.github/native-proof/legacy156_windows.py`; no historical helper is edited.
- Historical run40029884972 passed eight mixed-platform cases. Its Windows
  rows used Startup VBS and old `32d3e6728c71d1f7c8684b7b41ac1785cb8117a9`,
  not Suni's exact caller and not Troy's task shape. It is not current proof.

## Pending final combined-source gate

Parent requires the final source to include status-lock correction
`27f507acb4f12c30d237471095f137007952c312`; old-bdad7 checks are diagnostic only.
The next workflow refuses target preparation unless that exact repair is an
ancestor. Do not dispatch this staged successor with the old diagnostic pins.
Wait for the parent's exact combined SHA/tree, set `scope=full`, and run once.
No extra broad CI is started while the old diagnostics are still running.

`test_status_lock_windows_native.py` adds real Windows `os.fdopen(...,'r+')`
read/write/lock behavior on new and existing files, another process blocked by
the held native byte range, successful same-inode use after release/process exit,
and native hardlink/file-reparse/directory rejection with unchanged targets.
Run40052775499 executed these seven cases on exact27f507: **all seven passed**;
the complete native job had **63 passed,2 POSIX-only skips**. The update matrix
was deliberately skipped. These results do not prove Windows ACL isolation or
the Linux cross-UID result. They do not change ACLs, Developer Mode,
privileges or security policy; inability to create a required native object is
an unexercised/failed gate, not an automatic pass. The staged negative controls
also exercise the real `require_delivery_safe_update` SystemExit boundary while
their actual native children remain alive. The update driver refuses to proceed
if its native SCM inventory maps either installation shape to any service, and
requires the foreign Schedule host PID to remain unchanged through the final
90-second window. The SCM/unchanged-Schedule additions await the final combined update run.

## F1 correction: released install-to-update path (awaiting native execution)

`released_installer.py` now materializes **binary-exact**
`<frozen caller>:scripts/install.ps1`, verifies its independently recorded SHA256
(`53a81a8029ba670aed376ac9bc13906094cf12b63794993bb30fe6b60799c732`), and invokes
`powershell.exe -File <that file> -Commit <exact caller> -ForceCommit
-NonInteractive -SkipSetup -SkipComputerUse -Json`. All three released callers
have the same installer bytes. Candidate-to-next uses that same released1.5.9
installer with the explicit candidate as `-Commit`, not a modified candidate
installer. Missing bytes, an unknown/wrong frozen SHA, replacement refs or a hash
mismatch stop the fixture before any installation.

The driver first records the real `-Manifest`, then executes the **full installer**,
not selected install stages or a handmade uv environment. It requires every
manifest stage's successful, non-skipped frame, the final completion frame,
bootstrap marker/exact caller, installed artifacts, persistent user home/PATH,
and clean tracked source. Configuration and gateway autostart remain the
installer's documented non-interactive no-ops; fixture configuration and the
supported gateway registration follow. Computer-use setup and desktop building
are deliberately not certified. There is no fixture-owned dependency/recovery
fallback, source restore, lockfile repair or installer monkeypatch.

Installation uses the shipped default `%LOCALAPPDATA%\clover\clover-c1\venv`,
not the earlier synthetic source directory. Pinned uv0.13.5 is explicitly
preinstalled in the fresh home's managed `bin`; its bootstrap download is
**NOT EXERCISED**. The unmodified Python stage selects/provisions the released
3.11 default with managed-only discovery, rather than reusing a hosted image's
unrelated interpreter or forcing the old fixture's3.12 venv. The unchanged
`sqlite-before` gate still rejects an unsafe installed runtime. An installer or
SQLite failure remains a failed/unexercised update row, never a reason to bypass
installation or replace the interpreter.

The installer's two hardcoded GitHub Git URLs use process-local Git `insteadOf`
rewrites to a disposable bare mirror of the checked-out source. Its `main` is
the exact old SHA/tree, and all original objects/identifiers are retained.
`installer-origin-provenance.json` records both rewrites and the checked-out
source identity. This tests the real installer repository/checkout logic, not
GitHub authentication/download availability. Candidate objects are fetched only
after installation; the existing separate update origin still supplies the
unchanged target. No GitHub/main or released script is edited.

The historical F1 proposal used the public installed CLI `gateway install
--no-start-now --start-on-login` with a Limited/S4U request and an `n` UAC answer.
That premise did not establish a non-admin runner token. Its synthetic-token
successor then failed before the executing child wrote identity. Neither is native
acceptance. The real-account prerequisite above replaces those assumptions; the
full matrix remains blocked until account lifetime integration is reviewed.
The historical Troy task below remains a direct pythonw fixture, **not** the
public installed Scheduled Task path. Existing ownership, SCM/Schedule, native
security and completion assertions remain unchanged, including the90-second
stability window; they have not been waived to fit a detached public task.

After an explicit parent steer, a fresh PR150 read-back confirmed
`1a606496182ef48eafd78889eea8878165b829ae`; the read-only integration checkout
confirmed tree `43825538944a3d836e8a28ad509f39b0eca7710e`. The final `candidate` /
`full` pins now select that exact pair, not the earlier combined5bfc identity.
The old branch-only push trigger remains unchanged. Parent independent delta
approval still precedes publication and the one final native gate. This
correction has not been pushed, labeled, dispatched or native-certified.

## Bounded matrix

All update rows use real `windows-2025` hosted runners, a real product venv and
real processes. Zyra launches the product-generated Startup-folder VBS. Troy
registers a Limited/S4U task with the same native pythonw action/settings as the
previous supervisor proof, and independently requires Schedule ancestry before
updating. Schedule must remain running. The updater alone owns the successor.

| Caller | Exact from SHA | Shapes | Entries | Work at update |
|---|---|---|---|---|
| 1.5.6/Suni | `9535350bee1d57cf7e466672e6f416403886bec1` | Zyra, Troy | CLI, local-IRC handler | Accepted turn completed first |
| 1.5.8 | `0c8f3cd0e0ba6caca6948e13f9de92ba519c2029` | Zyra, Troy | CLI, local-IRC handler | Accepted turn completed first |
| 1.5.9 | `3b54f413376eb99f8b83bfd4a0a340d01825014d` | Zyra, Troy | CLI, local-IRC handler | Accepted turn completed first |
| candidate | exact candidate from pins | Zyra, Troy | CLI, local-IRC handler | Real provider request held in flight |

There are 16 unique rows, programmatically enumerated in the plan artifact.
The candidate updates to an ordinary, deterministic child commit adding only
`.native-owner-worker-next`. The recorded candidate is its exact parent. The
fixture supplies ordinary Git objects through a disposable bare origin; it does
not alias commits or publish marker refs.

Pass requires a current successful logical receipt with exact old/target source,
new owner identity, one gateway root (native launcher/worker pair is one gateway),
one live API listener owned by the status worker and stable identity for >=90s.
Each row executes one accepted tool action and receives one reply. It preserves
saved messages, kanban work, pending transcript content and a private skill;
it seeds a stale refused receipt and pending restart intent. Chat additionally
requires exactly one new received `Clover update finished` notice. Original
receipts, command logs, process topology, status and source identities survive
failure in artifacts. Rollback/large-state testing is NOT EXERCISED here.

A frozen Windows caller can stop the gateway before target code exists. The old
rows therefore do not claim protection of an executing action. A frozen 1.5.8
pre-pull capability refusal cannot be repaired by code it never loads: keep that
row red/unsupported, do not advertise automatic-update support or inject a flag.
Refusal is not acceptance even if all saved state survives. A failure before
source pull is labelled broadly until preserved output proves its exact cause.

## Independent native safety controls

`test_owner_worker_native.py` uses real native venv launcher/base-interpreter
children, genuine PID/birth observations and one/four separate health reporters.
It checks owner PID/start/code/capability preservation and concurrent health merge.
Negative controls retain missing/invalid status, mismatched birth (PID-reuse
witness), and foreign-home status without authorizing any kill. Discovery is
fixture-scoped, but process identity/status/admission are not fabricated. These
synthetic roles are not themselves an installed-gateway update proof.

The staged successor parametrizes all four unsafe-status conditions across the
three exact released callers and the candidate. `frozen_postpull.py` compiles
the original managed-uv import/call plus original outer handlers directly from
each immutable Git blob; native tests call the NEW library and require SystemExit
before dependency work, retaining both live children and private pending state.
This is a **post-pull excerpt seam**, not a full negative updater run or a way
around an immutable pre-pull refusal. These 16 expanded native seams are awaiting
the final combined-source gate; portable escape controls are not native passes.

## Baseline evidence (retained, not green)

- Initial run40048857495, workflow `bc530c70d00616bd6391bcc6510fde7a4dddd3ce`:
  fixture dependency error (`pytest-timeout` absent); no cases ran. Raw log kept.
- Corrected native baseline run40049065987, workflow
  `4daf7206e0d82f5dff4fb2068ca3554b3e2ed4f3`, product exact release1.5.9:
  canonical JUnit has **70 cases:64 passed,4 failed,2 skipped**.
- The new real reporter test reproduced owner loss: actual owner worker7344 was
  overwritten by reporter2112. This is the intended native RED.
- A separate native receipt-clock test also failed:
  `test_measured_windows_child_beacon_keeps_strict_birth_boundary[modern-inverted]`:
  recorded refreshed_at `1792804684.6441977` < measured birth `1792804684.644198`.
  Its strict assertion remains in the candidate lane. This is a separate blocker,
  not waived or blamed on owner repair.
- Two selected tests in `test_legacy_gateway_restart_idle_wait.py` assume POSIX
  SIGUSR1 and failed on native Windows. That POSIX file is no longer selected in
  this Windows lane. Its original failures remain in the baseline artifact/log;
  their removal does not remove the native clock or owner assertions.
- The baseline's update job is intentionally SKIPPED, NOT EXERCISED. No green
  release claim follows from the plan job.

- Candidate native safety on run40049337485: **58 cases:56 passed,2 skipped**;
  canonical JUnit/source-sha artifact verifies exact PR148. One/four real
  reporters retained owner6560/2492 respectively. The two skips are POSIX-only
  real stderr-wrapper tests, not Windows positives. The previously failing
  strict clock case passed this attempt; that is not proof of a clock fix.
- The 16 update jobs on that run all failed **before starting any gateway**:
  `sqlite-before` correctly rejected CPython3.12.12/SQLite3.50.4. The fixture had
  copied uv0.9.28 from a different OS-test workflow, not the proven native run.
  Raw logs/ZIP/artifacts are retained. No launcher/update row passed or ran.
  Historical run40029884972's actual setup log proves uv0.13.5 installed
  CPython3.12.15/SQLite3.53.1. The corrected update fixture pins that exact uv;
  the vulnerable-SQLite rejection is unchanged. That correction does not turn
  the failed run green; it is not a product/update pass.
- Corrected old-bdad7 diagnostics are retained in runs40049740582 and40050690083:
  eight Startup and eight Task cases passed across two fixture revisions. The
  first full invocation failed its Troy observer; the correction is explicitly
  partial. This aggregate is NOT one green full gate and cannot certify27f507.
- Standard CI40052777927 found five fixture encoding issues and one bare-uv
  lookup. The staged successor uses explicit UTF-8 and the pinned setup-uv
  action's verified `uv-path` output; it records binary hash/version and keeps
  the safe-SQLite gate. Local runtime-policy tests pass; hosted recheck is pending.
  No product exemption list or linter policy changed.

Local portable checks: `python .github/owner-worker-proof/test_contract.py`.
They validate immutable caller pins, 16-case uniqueness, refusal to substitute a
different 1.5.6 ref, and exact candidate-parent/marker-only Git behavior. They do
not certify Windows. This proof uses loopback IRC and a deterministic provider,
not live Telegram, real model credentials or a phone-visible transport test.
