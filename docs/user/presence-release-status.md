# Presence candidate verification — not a release approval

Presence remains **off by default**. This is a draft review candidate, not a
recommendation to update or restart. Its package version remains 1.5.6; no 1.5.7
release has been cut. Publishing or restarting does not enable the feature.

## Source identity

The combined product is `a16e9234eb494e0abcb85226f7df2f7ddd4cc6f1`, tree
`1c35d1628ebd8e7b7a85d6a44e5d85fa4848a319` (the draft PR records the final
documentation-bearing commit separately). Above the original joined candidate
`79e5df1357c0a7bd14788e512d30e2293731374f`, this joins independently approved
Windows reader `120708c9b04dfba6d31efacfef92c83cee749189`, macOS descriptor plus
quota repair `4b9007ef29d6c989fc95f8285628787ffd9f8dee`, and timing-only tests
`3282af6f75b2d1f0fbffb08522bfddb469368abf`. The composed guard retains native
Windows literal-NUL handling, Darwin kernel descriptor resolution and anchored
POSIX quota I/O. No CI-only helper ancestry enters this product.

Windows secure reader support does **not** enable Windows isolated quota writing:
that unsupported operation still holds before mutation or provider entry.
Production deadlines, Stop, owner checks and final publication fences remain
unchanged. The timing repair changes test preparation, not request/ACK budgets.

Combined execution exposed two narrow join corrections: missing POSIX UID
capability now refuses quota/write roots explicitly, and Windows read-only pipe
admission requires actual kernel pipe type and read-only granted-access rights.
Unresolved regular files, writable/unknown handles and character descriptors stay
denied. Native pipe construction does not permit subprocess entry or networking.
These new corrections require independent delta review; the original component
approvals do not silently cover them.

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
- The preceding joined 113-file selection on `79e5df13` completed with Linux
  green, Windows 26 failures and macOS two failures. These original receipts
  remain preserved; isolated component passes do not replace them.
- Reviewed component proofs admit these repairs for integration, not release:
  [Windows reader 39277475119](https://github.com/oaostarboy/clover-c1/actions/runs/39277475119)
  passed all 23 original Windows positives and real native adversarial controls;
  [timing fixtures 39275200026](https://github.com/oaostarboy/clover-c1/actions/runs/39275200026)
  passed the unchanged request/ACK budgets. Final quota raw receipts show Linux
  and macOS 42 cases each and one Windows unsupported-platform hold; anomalous
  queued/null workflow metadata is **not** an overall workflow green claim.
- The first approved-delta join `c17158f4` failed Windows preflight in
  [run39286577347](https://github.com/oaostarboy/clover-c1/actions/runs/39286577347):
  the original native reader file passed37 cases and failed one cold-selection
  assertion after three unresolved pipe-descriptor refusals. Full acceptance did
  not start; this is not a completed full affected-source execution. Unchanged-audit native
  diagnosis locates these at stdlib platform discovery's pipe construction.
- Tests-only native pipe controls on `af05a6af` reproduce two failures/four passes
  in [run39288618829](https://github.com/oaostarboy/clover-c1/actions/runs/39288618829).
  A separate full scanner found six bare UID expressions. Their fail-closed
  correction has a preserved three-failure/one-pass original capability control.
  Original failed logs and earlier transport-only incomplete/cancelled diagnostic
  runs remain preserved, not counted as behavioral passes or hidden retries.
- The fresh combined-source Linux/Windows/macOS matrix is **pending** at this frozen
  documentation snapshot. Consult the draft PR's exact-head receipts for later
  results; this page does not turn a workflow badge into platform clearance.
  Existing POSIX-only evaluator skips on Windows are explicit unsupported
  capability boundaries. The historical indented-stream timeout remains UNKNOWN
  and unwaived even if its unchanged full-context case passes later.

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
