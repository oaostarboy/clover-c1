# Presence candidate verification — not a release approval

Presence remains **off by default**. This is a draft review candidate, not a
recommendation to update or restart. Its package version remains 1.5.6; no 1.5.7
release has been cut. Publishing or restarting does not enable the feature.

## Source identity

The combined product is `10ab56fa15122de9eb0dfbe747495eb0fa439492`, tree
`02aabc6d0398cad138ac29d6f40da3fd53b7493b` (the GitHub review receipt records the
final documentation-bearing commit separately). This merges reviewed native tip
`6c072d6c54484f995ee2bbc3195ff52aa821b47b` and evaluation tip
`088e9b22feb0f041a96e7bd73ad2f4c12ba1fe51` above their common base. It includes
coherent SQLite read snapshots, private Windows cold-child fixtures, non-mutating
selected-owner reads, isolated evaluation quota, final-dispatch revocation checks
and bounded error diagnostics that refuse encoded bodies before decompression.
No CI-only proof workflow is part of the product integration.

## Component evidence is not combined-source acceptance

- [Native snapshot gate 39246893877](https://github.com/oaostarboy/clover-c1/actions/runs/39246893877)
  exercised earlier product `44ed7333`: Linux and macOS each completed 97 files,
  2,234 passes. Windows exceeded its unchanged 90-minute limit. Its available
  final receipts contained 1,675 passes, 10 failures and one existing POSIX-only
  skip; ten files lacked final receipts. The original failed/incomplete gate
  remains preserved and unwaived.
- [Windows fixture proof 39260962262](https://github.com/oaostarboy/clover-c1/actions/runs/39260962262)
  passed five files / 111 cases on `765e7514`, after causal HOME/import REDs.
  [Cold-child proof 39263058340](https://github.com/oaostarboy/clover-c1/actions/runs/39263058340)
  passed all 41 independent-review cases on `6c072d6c`. Assertions and safety
  guards were not weakened.
- Bounded native partitions completed the ten formerly missing files and the
  original owner file: 559 unique passing cases, explicitly mixed-source
  (518 on `44ed7333`, 41 on `6c072d6c`). This is not a combined-source gate.
  [Settlement evidence](https://github.com/oaostarboy/clover-c1/actions/runs/39260605707)
  and [remaining partitions](https://github.com/oaostarboy/clover-c1/actions/runs/39262303056)
  retain all earlier failures. Passing unchanged does not explain the original
  owner deadline or a later ten-second requested-completion failure sampled in
  required SQLite commits. Their causes remain unknown.
- The diagnostic correction has independent offline RED/GREEN review. Provider,
  quota, Stop, source and config fences remain required on the combined source.
- The combined-source Linux/Windows/macOS matrix is **pending** at this frozen
  documentation snapshot. Consult the draft PR's exact-head receipts for later
  results; this page does not turn a workflow badge into platform clearance.
  Existing POSIX-only secure-reader/evaluator skips on Windows are explicit
  unsupported capability boundaries, not Windows certification of that path.

## Historical and unexercised release gates

The 126 baseline-shared Linux whole-suite failures remain unwaived. Historical
macOS whole-suite failures/errors and incomplete Windows whole-suite execution
also remain unwaived. Passing an affected-source selection cannot clear them.
Installed Electron, native fresh installation, running-gateway update, rollback,
console and real behavior are separate acceptance gates, not proved by source
unit tests or a JavaScript build. No full release acceptance is claimed.

## Model smoke remains unresolved

The immutable global allowance records **one used, two remaining, one failed,
zero complete, zero uncertain, none in flight**. The sole real request returned
HTTP 400; its response body was not retained. The actual rejection cause is
**unknown**. Offline serializer comparison does not establish compatibility or
model quality. New bounded diagnostics cannot recover the historical body.

No live request, retry, fallback, credential probe, auth refresh, account/model
switch or live enablement is authorized by this candidate. Remaining allowance
is not retry consent. Scripted tests do not measure classification accuracy,
confidence calibration or useful thought extraction.

## Publication and restart boundaries

This draft is not merged main, a tag or a public 1.5.7 release. Independent join
review and required exact-source acceptance remain prerequisites. A fresh
isolated dependency environment may be prepared for import/dependency/rollback
checks, but it is **not selected for next start**. Preparation is not activation.
The running gateway, settings, authentication and live environment stay unchanged.
Do not restart or enable presence on the strength of this page.
