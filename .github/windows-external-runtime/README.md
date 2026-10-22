# Private native Windows external-runtime fixture

**Status: PREPARED, NOT NATIVE-EXECUTED.** Linux portable verifier checks are not
Windows acceptance. No live user install, bot credential, or public release is
used. Integration owner alone publishes the test-only helper branch.

## Frozen product

- Product commit: `c4338be28f3ad07f269f9b0090abdacee35cc979`
- Product tree: `51521e06f4150a375866c6148416aae2ac2ec68a`
- Existing-install source: `9535350bee1d57cf7e466672e6f416403886bec1`
- Workflow/helper commit: the private fixture commit, separate from product.
- Any new product pin requires repinning both workflow fallback identities and
  rerunning the affected native rows. This preparation proves no newer tree.

## Why the existing native workflow cannot cover this row

`native-exact-proof.yml` is registered and supports dispatch with old_ref,
target_ref, target_tree, and next_ref. Its Windows fixture explicitly installs
and probes `INSTALL_DIR/venv/Scripts/python.exe`. It has no input to select an
out-of-code editable environment. Do not report its fresh-install green badge
as external-environment migration proof.

The new file `windows-external-runtime-proof.yml` has a **push trigger** on
`ci/windows-external-runtime/**`. Do not assume a new workflow filename can be
manually dispatched before default-branch registration. The owner may instead
copy the job into an already registered helper workflow on a reviewed private
branch; this child does not modify that shared workflow.

## Probe rows (all pending native execution)

1. `existing-upgrade`: install the old source as a real editable distribution in
   an external venv using native CPython 3.11.9; seed real transcript and pending
   TodoStore messages; run the ordinary installed CLI from old source to the
   exact candidate, not a direct repair-function substitution.
2. `candidate-repair`: existing candidate installation on a measured vulnerable
   runtime; normal no-pull CLI update must repair it using real private uv
   provisioning, relocatable staging, locked dependencies and import smokes.
3. `holder-refusal`: keep an owned native interpreter holding real product
   imports; ordinary update must return nonzero, leave it alive, and retain the
   original environment and state. No force or guard bypass is permitted.
4. `provision-failure`: explicitly inject a provisioning exception inside the
   real CLI command. Require that the named boundary was reached and the CLI
   refuses to certify a measured unsafe unchanged runtime.
5. `smoke-rollback`: run actual candidate smoke checks, then deliberately reject
   the promoted candidate; write a new transcript and pending task after
   promotion but before rejection. Product rollback must retain that new state.

Positive rows also write new state after upgrade and manually roll the retained
external runtime back, without restoring or copying state.db. They assert new
state remains readable. **This manual runtime rollback is not an automatic
full-source rollback or restart claim.**

Each runner creates one source clone, external environment and data home under
a new RUNNER_TEMP subtree. It rejects reuse, personal/non-hosted execution and
non-Windows execution. The driver runs outside the mutable venv. Base Python,
SQLite/source ID, OS, selected updater runtime and gateway launch selector are
measured before/after. Correct SQLite backports are accepted through the
product safety predicate. Unsafe WAL fails the seeded-state gate.

The fixture does not fake os.name, simulate SQLite versions, disable holders,
force WAL, use replacement refs, relax installed lifecycle guards, or delete a
foreign environment. Fault injection appears only in named negative rows.

## Owner execution

1. Review the fixture commit and `SOURCE-PIN.json`; fetch the private fixture
   branch or apply the supplied patch in the owner's isolated CI publisher.
2. Publish that helper commit to a unique branch under
   `ci/windows-external-runtime/`. The push trigger runs five hosted jobs.
   Product default SHA/tree remain frozen above; helper commit is different.
3. Read back the remote helper SHA. Get the run ID with `gh run list --repo
   oaostarboy/clover-c1 --branch <helper-branch> --json databaseId,headSha,url,status`.
4. Keep one bounded watcher, retrieve all job IDs, download the five artifacts,
   save raw logs, and parse all five XML files. Require exactly five named
   testcases, zero skips/xfails and all expected state/identity/fault evidence.
   A workflow badge or a timeout without job logs is not a pass.
5. Record run URL, numeric job IDs, helper SHA, product SHA/tree, OS and native
   before/after versions in the parent ledger. Preserve failed attempts.

## Deliberately unclosed acceptance rows

This focused fixture does **not** start an installed gateway, deliver a turn,
exercise the Startup VBS or a Scheduled Task, or prove automatic restart with
one healthy gateway held for 90 seconds. Gateway *selector identity* is weaker
than a running process identity. It does not exercise actual Telegram transport.
The parent must extend the owned gateway fixture to the same external path and
prove in-flight turn survival and restart/refusal separately. No green result
from this helper alone authorizes a general-user release.

Source inspection supports editable git installs with one unambiguous PEP610
metadata record in their invoking venv; checkout-local venv/.venv remain
supported fallbacks. System Python, noneditable wheel-only installs, ambiguous
metadata, and environments without pyvenv.cfg are not certified by this slice.
Image/package-managed admission remains governed by the installed product.
Do not infer universal pipx/conda support from a directory name.
