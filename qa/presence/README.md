# W4 first QA slice — offline, blocked for release

## Outcome

Built an offline JUnit evidence checker using eight recorded RED/GREEN cycles.
It rejects empty/missing/duplicate/unexpected test identities, nonpassing cases,
incorrect suite totals, malformed reports/expectations and unsuccessful runner
exits. It **always returns `release_approved: false`**. It does not authorize a
release, authenticate evidence, change runtime policy or send anything.

Fresh-context review of the inherited S0 snapshot found three reproducible gaps:

1. Oversized integers raise `OverflowError` in affect admission (three fields).
2. Admission accepts a reading after its source row has been deleted.
3. Disabling presence during a source read still commits an internal offer.

All five adversarial cases remain **failing**, intentionally visible for the
owning builders. No production fixes were made. `review.json` contains source
locations, owners, impact boundaries and the reproduction command. These are
S0 findings only, not a review of concurrent W1/W2/W3 implementations. S0 never
publishes these offers; no outbound-message failure was demonstrated.

## Isolation and pin

The dispatch directory was not isolated. This worker subsequently created the
real worktree `/home/starboy/.clover/workspace/clover-157-presence-w4-review`,
branch `qa/clover-157-presence-w4`, from base
`9535350bee1d57cf7e466672e6f416403886bec1` and applied a read-only S0 snapshot.
The snapshot patch SHA-256 is
`577c313628f2fe8e14eceea70f85663faa6894ff9f3230f228177598fbe925da`.
All inherited candidate file hashes remained unchanged. Only QA files were
added by this worker. No commit, push, deploy, restart or live enable occurred.

## Verification

- Original focused suite: **73 passed**, no failures or skips, repeated.
- New checker suite: **29 passed**, with RED/GREEN logs for all eight cycles.
- New S0 adversarial suite: **5 failed** across the three findings above.
- Correct isolated production-module imports verified by path.
- Pinned upstream NEKO `LICENSE` and `NOTICE` match source copies byte-for-byte.
- The checker accepted the focused passing reports and rejected the adversarial
  report. Expected original-case identities came from the preceding unfiltered,
  source-pinned reference run, **not an independent collection oracle**.

## Reproduce

From this worktree, set `CLOVER_PYTHON` to
`/home/starboy/.clover/workspace/automatic-helper-checklists/release/stage/venv/bin/python`.
Run:

```
bash scripts/run_tests.sh tests/agent/test_presence_w4_receipt_gate.py -j 1 --file-retries 0 -v --tb=short
bash scripts/run_tests.sh tests/agent/test_presence_w4_adversarial.py -j 1 --file-retries 0 -v --tb=short
```

The second command should currently fail. Do not xfail or suppress these cases.

Raw commands, exit statuses, XML reports, logs, fingerprints and computed verdict
are under `/home/starboy/.clover/workspace/artifacts/clover-157-presence-w4`.
`qa-only.diff` applies only this worker's QA additions on top of S0.

## Still blocked

Builder fixes and recheck; combined-workstream integration; full canonical and
native OS acceptance; supported-distribution license audit; cross-model review
of both runtime and QA changes. Ordinary wheel builds are deliberately disabled
by this repository, so the attempted wheel build is **not** a packaging defect
and establishes no release-license coverage. Nothing here is a release sign-off.
