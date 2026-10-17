# Clover 1.5.6 automatic helper checklist candidate

Scope: checklist runtime and its launch/update/restart tests only. Base:
`d3b6bdeb56072e18b727c8a01f3582b0773e5136` from the freshly fetched
`oaostarboy/clover-c1` main branch. The parallel proactive-controls publication
owner keeps ownership of main, version/tag publication and deployment.

## Approved behavior

Ant's relayed approval: checklist visibility must not depend on the parent
remembering optional steps metadata. Previously, omission produced an empty
card and later native todos could not create rows. The runtime now seeds the
supplied structured step list or a minimal public-goal row, and observed native
todos can create missing rows. No new model request or public tool-schema field
is required. Explicit valid plans retain their previous precedence.

Before (no optional steps):

```
Audit checkout links
(no checklist rows)
```

After (accepted single-task dispatch):

```
Audit checkout links
▸ Audit checkout links
```

A queued child shows `○` instead of implying it has started. Later child todos
supply real detailed rows and their observed statuses. Stop, terminal fences,
native tool counts, card following, grouped views and short final cards remain
on their existing paths.

## Review and integration gates

1. Inspect the frozen commit, patch/bundle and file hashes in the accompanying
   `CANDIDATE.json`. Test receipts refer to that exact candidate tree; do not
   treat an earlier dirty snapshot as evidence for subsequent edits.
2. Review public-source selection, id-versus-label matching, per-task ownership,
   early-event terminal fencing and the consumed internal resume scope. In
   particular, private worker context and unrelated parent todos are not UI
   plan sources.
3. Read actual serialized Telegram Bot API proof in the accompanying wire
   receipt. Tests use real launch/store/relay/publisher/editor/adapter/PTB paths;
   only credentials, model/child work and the network endpoint are fake.
4. Read the full bounded affected-suite receipt and exact unchanged-base failure
   comparison in `RESULT.md`. Do not call a broad run fully green if it contains
   known base failures. The entire repository suite and a Windows console matrix
   are not claimed by this scoped runtime change.
5. Obtain independent source review, integrate with the publication owner's
   candidate, and rerun the affected acceptance paths on the final combined
   tree. Parent/owner approval still controls a local restart, live phone
   screenshots, main/tag publication and fleet rollout.

Nothing in this handoff authorizes publication or deployment. This branch does
not change providers, accounts, selected models, live configuration or the
release version. A frozen candidate is not a shipped or phone-accepted fix.

## Corrective review scope

The independent review of `e75ef5dd8222ef2520962b6afdb883a5e3f6e11f`
requested two corrections: strict inline-assignment privacy at the public
checklist boundary, and exact saved identity/status initialization on resume.
The corrective candidate retains those six failing reviewer cases, expands the
wire/public-persistence tests, and keeps the original 35 real-path tests intact.
Its exact source identity and bounded results are in the separate correction
`CANDIDATE.json` and `RESULT.md`; historical receipts are not final-tree proof.

A separate test-only commit excludes generated `__pycache__/*.pyc` files from
the fixed-shop fixture's source inventory. Its exact config/source-change
assertions remain unchanged; unexpected source/text files still participate.
The baseline and candidate both reproduced that precompile artifact on locked
Python 3.11. Workspace admission tests use neutral temporary roots, without
removing git markers or weakening admission. No owner-pairing, renderer design,
live settings, installation, restart or publication changes are included.
