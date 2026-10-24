# Windows owner/worker proof (test-only)

## Scope and pins

This branch owns only the new proof directory and workflow. No product edits,
installation into the live environment, release, merge, credentials, service
permission changes, Git replacement refs, refusal retry, or driver-owned restart.

`pins.json` is the versioned parameter handoff: exact candidate SHA/tree, exact
released callers, and the historical helper SHA. A push executes those pins, not
whatever main happens to contain. Keep `mode=baseline` for a release-only native
safety run; set `mode=candidate` with an independently verified new SHA/tree for
the full matrix. This new workflow intentionally is not registered on main.
Parent review is required before adding `ci-reviewed` or merging the fixture.

**Current pins select `native-safety` only on reviewed correction27f507.** The
updater matrix is intentionally SKIPPED/NOT EXERCISED in this invocation, and
the planner cannot label it a full updater gate. Both old-source diagnostic
runs have terminated; this narrow run exercises the new descriptor/security
cases without duplicating an active matrix. The explicit historical eight-row
`troy-verifier-correction` scope is restricted to old bdad7 and cannot clear a
future source. For the final combined gate, set `mode=candidate`, `scope=full`
and the parent's exact new commit/tree; require all16 rows from that invocation
rather than adding passes from different fixtures.
The revised observer copies the proven PS probe's exact installed-image ->
direct-child -> measured-base-image relationship. It additionally records raw
process/status evidence, requires the status owner to be that worker, rejects
extra gateway processes, and retains native birth, listener and source checks.
No process environment or argv prefix can substitute for those witnesses.

- Candidate received: PR148, `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`.
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
These staged tests are **NOT NATIVE-EXECUTED YET**. They do not prove Windows ACL
isolation or the Linux cross-UID result. They do not change ACLs, Developer Mode,
privileges or security policy; inability to create a required native object is
an unexercised/failed gate, not an automatic pass. The staged negative controls
also exercise the real `require_delivery_safe_update` SystemExit boundary while
their actual native children remain alive. The update driver refuses to proceed
if its native SCM inventory maps either installation shape to any service, and
requires the foreign Schedule host PID to remain unchanged through the final
90-second window. These additions are likewise awaiting the final native run.

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
  the vulnerable-SQLite rejection is unchanged. A corrected native run is
  required; this is a tool-catalog correction, not a product/update pass.

Local portable checks: `python .github/owner-worker-proof/test_contract.py`.
They validate immutable caller pins, 16-case uniqueness, refusal to substitute a
different 1.5.6 ref, and exact candidate-parent/marker-only Git behavior. They do
not certify Windows. This proof uses loopback IRC and a deterministic provider,
not live Telegram, real model credentials or a phone-visible transport test.
