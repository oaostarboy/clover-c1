# Clover release changelog

Short notes shown after `/update` live in [RELEASE_NOTES.md](RELEASE_NOTES.md).
This changelog records the broader scope and limits of the 1.5.6 release.

## Unreleased

- Keep the default `/proactive` menu phone-sized; put technical diagnostics in
  `/proactive status`. Native controls remain available only to explicitly
  enrolled admins. Read-only replies explain missing enrollment and link to
  scoped local setup with its slash-policy side effects; chat admission is not
  silently promoted to admin authority. Callback replies remain compact and
  distinguish saved from effective state under managed policy.

- Add `/proactive` as a deterministic current-profile settings menu, with
  owned, expiring Telegram Enable/Disable buttons and explicit text controls
  on other gateways, CLI and TUI. Show saved and effective settings; workstream
  readers pick changes up at the next turn/operation without a restart.
- Enabling selects active recall/correction and deterministic compaction audit
  without granting new model-call, scheduling or external-action authority.
  Disabling preserves records, preferences and independent scheduled jobs.
  Gateway writes require an explicitly configured, authenticated admin.
- Remove only the old `/proactive` alias for `/loop`: legacy interval/prompt
  syntax gives migration instructions, never executes or toggles. `/loop`
  behavior and existing loop records are unchanged.

## 1.5.6 — 2026-10-16

### Work continuity and truthful completion

- Optional, per-profile unfinished-work tracking preserves scoped obligations,
  corrections, relevant context and typed evidence through supported compaction
  and restart boundaries. It remains off by default; updating enrolls no jobs.
- Exact source/job/session identity keeps unrelated work with identical wording
  separate. Replayed results do not add duplicate notes. Reading or delivering
  helper output is not independent verification of its work.
- Project pytest reports remain unverified evidence. A passing-looking report
  cannot close review, installation, release or delivery obligations. Narrow
  runtime-owned digest audits retain their separate, bounded meaning.
- Authenticated human ingress, owner/platform isolation, Stop/revocation and
  existing permissions govern corrections and linked blocker continuations.
  Clearing a blocker does not create new authority.
- Relevant recall, receipt-backed optional offers and reviewable procedure
  drafts use existing owners. Procedure drafts still need human approval.

### Promised reply fixes

- Preserve direct answers already written in the current turn when a helper
  takes over the remaining task. Do not duplicate an answer already delivered.
  This cannot manufacture an answer the model never wrote.
- Consume a proven-successful council route-check completion after the council
  already delivered its answer, without waking a redundant model response.
  Real failures, unknown delivery and new findings still notify normally.

### Install, update and verification fixes

- Windows Node dependency installation uses the committed lockfile with bounded
  cleanup instead of rewriting tracked dependency files.
- Windows update dependency handoffs preserve the current owned receipt. Modern
  heartbeat timestamps cannot precede measured child birth; immutable legacy
  producers require independently proved original parent phase and identity.
  Reader ownership, source and freshness checks remain fail-closed.
- Native Windows work-record reads reject unsafe file objects and concurrent
  writers using platform primitives rather than pretending POSIX rules apply.
- CLI context-only callers do not create authenticated human-ingress effects.
- SQLite fault-injection, startup isolation and event-driven deadline tests
  exercise real reader/owner seams. The UI elapsed-time regression waits for
  the actual reveal refresh rather than letting a periodic tick mask failure.
- Exact-target native proofs retain installed-source/import identity, real
  running-gateway updates, one current-run receipt and a stable health window.
  Windows fixtures preserve PowerShell module locations, official UCRT64 Git
  Bash layout and UTF-8 proof logs. Report-only batching keeps the original
  deadline and case set; it does not waive failures.
- Docker tests use confined grants for inspected, owned containers and cleanup,
  not a global live-system safety bypass. Both image architectures require an
  actual build and integration run; a classifier-only badge is not build proof.

### Inherited, not patched again

The vanished-working-folder terminal recovery and distinct automatic-handoff
names, full task context and default-tier handling shipped in **1.5.5**. They
remain part of 1.5.6 through ancestry, without duplicate patches.

### Evidence boundaries and known gaps

- One authorized HTML job completed build, checks, independent SOL review,
  final checks and delivery without a continuation nudge. This proves bounded
  follow-through for that job, not guaranteed autonomy, idle initiative, or
  comprehensive live Stop/restart recovery.
- Native release checks cover fresh Linux/macOS/Windows installs and real
  updates with a running gateway. Windows chat proof uses disposable local IRC,
  not a claim about every Telegram account, provider or user machine.
- Multi-helper raw formatting and split photo-album delivery were reported;
  **this release does not claim those issues are fixed**.
- Historical slow report-only runs and a first cold Windows process-query
  timeout remain preserved as failures. No unsupported cause, query deadline
  increase or blanket immunity from infrastructure failures is claimed.
- Publishing enables other users to obtain the update. It does not change the
  maintainer's active trial, enable proactive mode or roll out another profile.

See [the workstreams guide](docs/user/proactive-workstreams.md) for exact
supported grammar, settings, permissions and feature limits.
