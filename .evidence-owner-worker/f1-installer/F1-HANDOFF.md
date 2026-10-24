# F1 installer fixture correction — ready for independent delta review

## Outcome and authority boundary

The fixture now invokes the actual, hash-bound released `scripts/install.ps1`
before old-source identity and frozen-caller updates. It no longer manufactures
that installation with direct uv venv/sync or private Startup-entry generation.
This is a fixture correction, **not native Windows certification or reviewer
approval**. No installer ran on this Linux host. No push, CI label, workflow
dispatch, merge, release, live config/key/runtime operation or final gate occurred.

Review the correction against fixture base
`8883828f78049272e7c98b590e142a8ec56d8359`. Only the owned proof/workflow and
these local evidence paths changed; do not integrate unrelated inherited product
history as a fixture delta.

## Exact source pins

After the parent's explicit current-head steer, a fresh PR150 read-back confirms:

- Product: `1a606496182ef48eafd78889eea8878165b829ae`.
- Product tree: `43825538944a3d836e8a28ad509f39b0eca7710e`.
- Required ancestor27f507 exists in that target.
- `pins.json` explicitly selects `mode=candidate`, `scope=full`, this SHA/tree.
- Three frozen caller SHAs and historical helper93cf13 are unchanged.
- Fixture revision remains independent of product pins. The branch-only push
  trigger still names the existing proof branch; this work is unpublished.

The old5bfc candidate and earlier diagnostic/native results remain historical;
none were relabeled proof for the newly pinned combined tree.

## Correction details

- The three released Git blobs are byte-identical, SHA256
  `53a81a8029ba670aed376ac9bc13906094cf12b63794993bb30fe6b60799c732`.
  The helper copies binary bytes, checks them immediately before each real
  `powershell.exe -File` invocation, records source ref/blob/caller/tree, queries
  the real manifest and executes the full non-interactive installer.
- Required stage frames, terminal completion, exact bootstrap marker, default
  installed artifacts, tracked-source identity and persistent user home/PATH
  must all verify. Supported `SkipSetup`, `SkipComputerUse` and non-interactive
  gateway behavior are disclosed; fixture configuration/registration follow.
- Installation uses `%LOCALAPPDATA%\clover\clover-c1\venv`. The released Python
  stage provisions/selects its normal3.11 default with managed-only discovery;
  there is no fabricated external venv or forced3.12 substitution. The original
  safe-SQLite check remains mandatory. A real installer/runtime failure stays red.
- Exact uv0.13.5 is explicitly preinstalled in the fresh managed bin and hashed.
  Installer uv bootstrap download is **NOT EXERCISED**. Other hosted-image tool
  detection/download behavior is recorded in the actual installer log, not
  certified from preinstallation.
- Both hardcoded GitHub Git URLs use process-local `insteadOf` mappings to a
  private bare mirror, whose main is the exact old caller/tree. This preserves
  objects and commits and leaves released scripts unchanged. Download-source
  provenance is recorded; GitHub authentication/network clone availability is
  not certified. A real file-transport-only Git contract exercises both rewrites.
- The supported installed `gateway install --no-start-now --start-on-login`
  CLI registers Startup under a genuine Limited/S4U token and a supported UAC
  decline. The registration helper records identity, output and task XML/result,
  fails if administrative, and removes only its temporary registration task.
  Zyra launches the registered VBS; Troy retains its separately proven legacy
  native pythonw Task shape, not a false installer-default claim.
- New installer/harness source hashes, full raw logs, frames, mirror provenance,
  installer completion/registration, original SQLite gates and existing receipts
  remain available in the per-row evidence. No fixture-owned install/restart
  fallback, lockfile repair, source restore or product monkeypatch was added.

## Preserved acceptance

Portable retention checks verify the reviewed bootstrap, frozen exception seam,
planner, Troy script, topology and native safety files retain their original
checkout bytes (including the existing `.ps1` CRLF attribute). Native safety job
selection/content and owner/PID/birth/listener/Schedule admission functions remain
unchanged. The entire update/turn/receipt/completion/stability/recovery acceptance
suffix is unchanged. All16 frozen negative post-pull controls remain intact.

The same16 caller/setup/entry rows remain planned. Native ACL/object/ownership,
foreign Schedule PID,90-second one-gateway stability, one accepted effect/reply,
carrier ACK/completion, state/customization and stale-intent assertions were not
waived. None of these have been represented as newly passed Windows cases.

## Executed local checks

- F1 RED: the new installer-before-update contract failed on the reviewed base
  because no released installer invocation existed (`red.log`).
- Final portable pytest: **20 tests +21 subtests passed**; canonical JUnit has
  **41 entries,0 failures,0 errors,0 skips** (`portable.xml`, `portable.log`).
- Real Git rewrite controls clone both released URL forms through local file
  transport with every network protocol prohibited, preserving commit/tree.
- `compileall`, scoped Ruff and `git diff --check` passed. Current target/tree,
  ancestry, distinct16-row planner and retained native functions/job were checked
  by `verify_local.py`; see `local-result.json` and `plan.json`.
- Two test-authoring errors are preserved separately, not product REDs: the
  non-Windows refusal test initially supplied the wrong argument count; the
  retention test initially compared a raw LF Git blob with the already-CRLF
  unchanged Troy checkout. The corrected retention check uses Git's exact
  checkout filters. The released installer still uses raw Git bytes/hash,
  never newline normalization.
- No PowerShell executable exists here. Actual installer, limited-token CLI
  registration, SQLite selection and all native acceptance remain unexecuted
  until the parent-approved Windows gate.

## Exact next native execution contract — DO NOT DISPATCH BEFORE REVIEW

Workflow: `.github/workflows/windows-owner-worker-proof.yml`.
Versioned inputs: `.github/owner-worker-proof/pins.json` above, `full` scope;
planner artifact enumerates all16 rows. The existing trigger is push-only to
`clover-subagent/subagent-sa-0-af0ff5ad`; there is no workflow_dispatch input API.
Parent must independently approve/integrate this fixture delta before publishing
that proof revision or otherwise authorizing its one final native invocation.

The workflow supplies `CANDIDATE_SHA=1a606496182ef48eafd78889eea8878165b829ae`,
row `OLD_SHA/ENTRY/SETUP/TURN_MODE`, historical helpers, setup-uv0.13.5's absolute
`BOOTSTRAP_UV`, and `UPDATE_TARGET/UPDATE_TREE` from the existing target preparer.
Candidate-to-next remains an ordinary marker-only child of the exact candidate.
Its native row command is:

```bash
uv run --no-project --with psutil python helpers/.github/owner-worker-proof/windows_update.py \
  --source "$GITHUB_WORKSPACE/source" --old "$OLD_SHA" --entry "$ENTRY" --candidate "$CANDIDATE_SHA" \
  --setup "$SETUP" --turn-mode "$TURN_MODE" --target "$UPDATE_TARGET" --tree "$UPDATE_TREE" \
  --evidence "$RUNNER_TEMP/evidence" --uv "$BOOTSTRAP_UV"
```

A failed real installer/SQLite prerequisite must retain its actual failure. Do
not restore direct uv installation, change runtime selection to manufacture a
pass, weaken any native assertion, or claim user update readiness from these
portable results. The parent owns bounded independent review, then the final
approved real-Windows execution and canonical evidence read-back.
