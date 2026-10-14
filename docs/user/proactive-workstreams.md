# Proactive workstreams

An **opt-in**, per-profile ledger that keeps unfinished work, your corrections
and verified progress across new chats, restarts and compaction. It is stored
in the existing `cron/notepad.db` and is **off by default**: with
`proactive.enabled: false` no workstream tables are created, nothing is added
to prompts, and no extra model call is made.

## What it records

| Record | Meaning |
| --- | --- |
| obligation / blocker | Work that failed, was not run, is still running or was stopped, with the evidence that says so |
| correction / decision / constraint | Facts you stated; a correction supersedes the older fact and keeps its provenance |
| progress | Verified, finished work (kept distinct from outstanding remediation) |
| procedure | A reviewable skill draft proposed after repeated verified success |

Every record belongs to one *scope*: profile + project + audience + owner
root. Nothing crosses scopes, and group/shared audiences are skipped while
`private_only` is true.

## Receipts are facts from the owning subsystem

`executed`, `saved`, `delivered`, `scheduled`, `authorized`, `proposed`,
`deferred`, `cancelled` and `unknown` are separate receipts. Each is written
only after the owner confirms it (execution ledger, durable delegation ledger,
file read-back, scheduler occurrence). Model text is never a proof. Plain
outcomes shown to you: *draft ready*, *saved locally, not delivered*,
*waiting*, *failed*, *delivery unknown*, *delivered*.

## Feedback and timing

Explicit feedback (`accept`, `reject`, `correct`, `defer`, `done`, `stop`) is
typed and attributed to you. **Silence is unknown, never a rejection.** The
deterministic gate checks relevance, scope, duplicates, cooldown, your timing
preference and a per-session cap; it (and any optional judge) can only
*suppress or defer*, never authorize. With no timing preference, unsolicited
offers are deferred.

## Procedures

A draft is proposed only after at least 3 distinct, owner-confirmed
executions across at least 2 sessions, each with transferable steps. It is
staged through the normal skill write-approval store (`/skills pending`) and
is never installed without your approval.

## Inspecting from the CLI

```
clover cron workstream <project> list
clover cron workstream <project> show <id>
```

Only these two read-only commands exist. `feedback`, `approve`, `prune`, `link`
and `unlink` are refused before anything is read or written: they record a user
decision or change durable state, and a terminal cannot prove a human is at it
(an agent's shell can allocate a PTY). They are carried by the chat ingress once
it records a trusted human-origin receipt for the message; until then nothing
persists them.

`<project>` is the project's directory (`.` or an absolute path is resolved the
same way chat turns resolve it) or a literal project key; `--root` defaults to
`local`, the root CLI/TUI chat turns use. Listing and showing work even after
the feature is disabled.

### Corrections and feedback

Use a plain-text message that starts with `Correction:` to record a correction.
To supersede a specific record, include its ID after the prefix, for example
`Correction: ws_0123456789abcdef: use staging-2`. Feedback uses
`Feedback: ws_<16 lowercase hex characters> <kind>`, where `<kind>` is
`accept`, `reject`, `correct`, `defer`, `done`, or `stop`; an optional note can
follow a colon, for example `Feedback: ws_0123456789abcdef defer: not now`.

These operations are recorded only when the stored message row carries the
session store's trusted human-origin receipt (written by the real user-input
ingress and bound to the profile, session, row, and event). A database role of
`user`, a terminal, assistant or tool text, quoted or replayed rows, and
silence are not evidence. Leading-slash forms such as `/correct` and
`/feedback` are rejected as gateway commands and do not reach this consumer.
Secrets are redacted; paths, IDs and test names are kept literally;
superseded records keep their history.

### Linking a monitor job

A monitor job can be linked to one approved, open record so changed monitor
output is recorded as evidence. Linking needs a verified human-origin receipt
and an enrolled job (`proactive.eligible_jobs`) whose `workdir` equals the
project, so it is unavailable until the chat ingress provides that receipt. It
never starts, authorizes or schedules work.

### Procedure evidence

A run counts only when the foreground execution owner reads authentic stored
terminal command/result rows with successful exit codes and transferable
steps. Receipts bind the original session, row interval and exact scope;
compression-rotated segments remain one conversation. Three distinct verified
executions across at least two conversation roots can stage one attributed
draft. Delegation completion and cron transport success are not procedure
execution evidence. Model prose, failed/unknown results, repeated receipts,
foreign scope and invented sessions never count. Draft staging is not skill
installation and still needs explicit human review.

### Supported surface and operation replies

The combined candidate tests authenticated Telegram private plain-text ingress
for these explicit markers, not universal natural-language understanding.
Correction/feedback replies use the persistence owner's inline response:
`Recorded ...` or `Not recorded ...`, never an unverified model save/delivery
claim. Ordinary replies and provider history are unchanged. A monitor request
uses exactly `workstream-monitor: ` followed by a JSON object with `scope`
(`profile`, `project`, `audience`, `root`), `workstream_id`, `prompt`, `schedule`,
`monitor_script`, and boolean `approve: true`. All scope values must match the
current authenticated surface literally. It creates a local-only job, not
enrollment, linking or continuation permission. After independent job
enrollment, a new current message repeats the original request plus the actual
`job_id` to link that same session owner's job. Other DM/group/foreign scope,
model/tool text, historical replay and malformed identifiers cannot adopt it.
Denied/unknown operations are reported without a new send route; ambiguous
partial persistence requires inspection, not automatic retry.

Suggestions require explicit `proactive.suggestions.preferred_timing:
after_reply`; they are optional inline local artifacts, not delivery proofs.
Recall uses bounded lexical triggers; semantic/paraphrase completeness is not
claimed. A known missed-need paraphrase remains measured in the test fixtures.
Only supported native text-summary compaction is audited; opaque provider
compaction is not. The durable scoped capsule is independent evidence.

This is an unapproved opt-in candidate, not a released or enabled capability.
All seven local acceptance contracts and independent exact-candidate review
are required together. Windows/macOS, live providers/transports,
phone/desktop/TUI suggestions and opaque-native gates are not certified by
the isolated Linux scripted-provider tests. Procedure provenance includes a
source builder model-substitution exception; separate exact-model review and
final independent Fable closure remain required before release.

## Settings

See the commented `proactive:` block in `cli-config.yaml.example`. Defaults:
everything disabled, `mode: shadow`, thread cap 16 items / 4096 bytes,
30-day retention, recall 3 items / 1200 bytes, cooldown 3600 s, 1 offer per
session, procedures need 3 runs / 2 sessions, auxiliary calls 1 per event /
4 per day / 512 output tokens.

## Rollback

Disable `proactive.enabled`. Rows stay inert and inspectable; no data is
deleted and nothing is restarted. A database written by a newer build is
refused (fail closed) rather than modified.
