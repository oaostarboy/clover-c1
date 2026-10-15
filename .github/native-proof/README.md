# Exact-target native proof support

**Separate CI review is required before publication or dispatch.** This directory
adapts the existing shell installer, Windows supervisor proof, and local IRC
fixture. It does not change runtime code or certify a native run by existing.

## Scope and evidence

`native-exact-proof.yml` accepts `old_ref` (default `v1.5.5`), an immutable
`target_ref` + `target_tree`, and a unique `ci/native-next/<name>` branch.
The next commit must have exactly one parent (the target) and change only
`.github/win-e2e/NEXT_MARKER.txt`. Pins resolve once, then every native row
rechecks them. A mismatch fails. No replacement refs, grafts or alias commits
are created. No workflow writes remote refs or requests repository secrets.

Rows are separate: Linux/macOS/Windows fresh installs plus native SQLite,
report-only, Stop and restart fixtures; Linux/macOS gateway-mode CLI updates
in both directions; all eight Windows Troy/Zyra × CLI/local-chat ×
old2new/new2next rows. Every update must start with the original gateway
running and end with one new gateway on the expected source, owning the API
port, running with no restart request for 90 seconds. The updater must write a
fresh final success receipt bound to the exact pre/post commits. Windows chat
also requires its final reply in the current disposable IRC transcript.

The download topology is deliberately a **local bare-origin fixture**: its
`main` holds the actual candidate/next commit objects. `origin` and `upstream`
point there. This lets the unchanged `/update` main path run without modifying
GitHub/main or misidentifying code. It is a local-fork update proof, **not an
actual GitHub/main download or Telegram transport proof**. The installer still
fetches the official repository at the pinned starting commit. No driver-side
restart/recovery can turn a failed updater into a pass.

`identity` runs outside the source root with no PYTHONPATH. It records actual
HEAD/tree, package origins, and the installed distribution's explicit
`direct_url.json`. Existing shared package origins work on the old release,
which predates proactive modules. Candidate feature tests run in the installed
interpreter via the canonical guarded, per-file runner; every selected file
must exist, execute cases and produce non-skipped passing JUnit. Retries are
off. The dev test dependencies use the installer's managed uv with an explicit
installed interpreter (installer venvs are not assumed to contain pip). Raw exit codes, per-suite JUnit, source observations, product receipts,
gateway windows and workflow/run/attempt/row outcome are retained, including
failures. Scripts refuse native startup outside disposable hosted runners.

The old Windows venv-holder gate remains **blocked**, not waived: preserved
candidate/base runs each have the same five guard rejections and six passes.
Its cause lives in `tests/conftest.py` / an existing non-owned test, outside
this lane. This workflow neither bypasses that protection nor claims it fixed.

## Local validation

```bash
python .github/native-proof/test_contract.py
/path/to/actionlint .github/workflows/native-exact-proof.yml
bash -n .github/native-proof/posix.sh
pwsh -NoProfile -File .github/native-proof/test_windows_contract.ps1 -Python "$(command -v python)"
git diff --check
```

No native acceptance is claimed by these portable tests or syntax checks.
The test fixtures contain deliberately synthetic data only for negative
contract tests, never as replacement native-run receipts.

## Parent-only next stage (AFTER independent CI review)

1. Review the exact narrow commit; integrate runtime lanes and get a fresh
   exact combined source review. Original source approval does not cover edits.
2. Publish the reviewed **CI-only diff** through its own reviewed PR onto the
   default branch first. A new manual workflow must exist on the default branch
   before GitHub registers `workflow_dispatch`. Do not merge candidate product
   code merely to register the verifier. Only the parent/maintainer may approve
   the `ci-reviewed` label, after review; this builder has not set it.
3. Set these variables to the actual reviewed integration worktree and reviewed
   workflow ref. The following commands publish frozen proof refs and dispatch;
   they have **not been run** by the builder.

```bash
# Set to real paths/refs selected by the parent; no unreviewed publication.
: "${INTEGRATED_TREE:?exact parent integration worktree required}"
: "${WORKFLOW_REF:?reviewed workflow commit/branch on GitHub required}"
: "${PROOF_NEXT_DIR:?new private scratch worktree path required}"
TARGET=$(git -C "$INTEGRATED_TREE" rev-parse HEAD)
TREE=$(git -C "$INTEGRATED_TREE" rev-parse 'HEAD^{tree}')
RUN=$(date -u +%Y%m%dT%H%M%SZ)
TARGET_BRANCH="candidate/native-proof-$RUN"
NEXT_REF="ci/native-next/$RUN"
git -C "$INTEGRATED_TREE" worktree add --detach "$PROOF_NEXT_DIR" "$TARGET"
git -C "$PROOF_NEXT_DIR" switch -c "$NEXT_REF"
python -c 'from pathlib import Path; import sys; p=Path(sys.argv[1])/".github/win-e2e/NEXT_MARKER.txt"; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.argv[2]+"\n")' "$PROOF_NEXT_DIR" "$RUN"
git -C "$PROOF_NEXT_DIR" add .github/win-e2e/NEXT_MARKER.txt
git -C "$PROOF_NEXT_DIR" commit -m "ci: native next marker $RUN"
NEXT_SHA=$(git -C "$PROOF_NEXT_DIR" rev-parse HEAD)
git -C "$INTEGRATED_TREE" push origin "$TARGET:refs/heads/$TARGET_BRANCH" "$NEXT_SHA:refs/heads/$NEXT_REF"
git -C "$INTEGRATED_TREE" ls-remote origin "refs/heads/$TARGET_BRANCH" "refs/heads/$NEXT_REF"
gh workflow run native-exact-proof.yml --repo oaostarboy/clover-c1 --ref "$WORKFLOW_REF" \
  -f old_ref=v1.5.5 -f target_ref="$TARGET" -f target_tree="$TREE" -f next_ref="$NEXT_REF"
gh run list --repo oaostarboy/clover-c1 --workflow native-exact-proof.yml --event workflow_dispatch --limit 5
# Resolve the exact new run ID by workflow SHA + inputs before watching it.
: "${RUN_ID:?exact newly dispatched run ID required}"
gh run watch "$RUN_ID" --repo oaostarboy/clover-c1 --exit-status
gh run view "$RUN_ID" --repo oaostarboy/clover-c1 --json headSha,conclusion,jobs
gh run download "$RUN_ID" --repo oaostarboy/clover-c1 --dir "native-evidence-$RUN_ID"
```

Permissions: parent write access for CI/proof branches and CI-only PR; workflow
scope when pushing workflow files with a classic PAT; Actions write to dispatch
and Actions read to collect evidence. Hosted Windows Scheduled Task creation
uses only the disposable runner account. No real-user messaging or provider
credentials are needed. Check every executed row and receipt against the final
candidate; skipped/cancelled/failed rows are not passes. Required PR CI and
independent review remain separate gates; no merge/tag/release/live update is
authorized by this verifier's green result alone.
