# Reliability-only private candidate

This candidate retains the 1.5.6 baseline version and does not include the archived
emotion/presence candidate. It is not a deployment or a release.

- Telegram reconnect recovery now reclaims this runtime's exact `Not connected`
  rejections after adapter replacement, using the existing owner/profile/attempt
  fences and visible recovered-reply marker. A transport failure after a possible
  remote acceptance is not silently retried. Already-sent chunks are retained as
  evidence and prevent whole-response retries. Ambiguous restart redelivery still
  uses the existing visible duplicate-warning contract; this is not exactly-once.
- Delegation lifecycle status and `exit_reason` are unchanged. Additive
  `task_outcome` carries self-reported `done`, `partial`, `blocked`, or `unknown`,
  with `remaining`, `owner`, and `next_step` for structured reports. The child emits
  `Task outcome: {"status":"blocked","remaining":["approval"],"owner":"human owner","next_step":"Approve the bounded check?"}`
  before its Result line. A single exact stand-alone legacy Result line remains
  supported. Quoted prose and malformed/conflicting reports are not success proof.
  Partial/blocked/unknown are not rendered as successful task completion. The
  unified card retains its internal failed terminal bucket for these outcomes but
  shows their reason; the activity tracker uses incomplete. Neither changes the
  transport contract or creates automatic retry rights.
- Existing accepted staged plans still continue actionable partial results once,
  through the original owner and all scope/Stop/route/approval fences. A reported
  blocker preserves the durable unconsumed next stage and asks for its specific
  missing decision. Step/retry exhaustion stops the chain and escalates with saved
  follow-up data. No accepted next action means no new project or worker.
- Google Workspace `--preflight` is read-only dependency metadata inspection.
  Missing dependencies exit 2 without pip/uv mutation. Explicit installation
  requires running the intended project's venv Python and passing the identical
  absolute path using `--install-deps --python PATH`. Auth checks can still refresh
  tokens and are not the read-only preflight. Installed user/profile skill copies
  are not automatically changed by this repository patch.
- Reviewed storage recovery is extracted independently of emotion code. It retries
  persistence, never providers or tools, retains uncertain pending evidence, retires
  successful sidecars to metadata, and uses reviewed directory anchoring/barriers.
  No live journal changes, DB migration, runtime upgrade or all-opener cutover is
  included. Native Windows/macOS and installation/backup/rollback acceptance remain
  separate gates. Stop and owner reconciliation remain authoritative.

Offline fake transports and synthetic databases prove only these tested boundaries,
not live service recovery, real provider delivery or native platform support.
