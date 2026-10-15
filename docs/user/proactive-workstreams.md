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

### Creating an explicit blocker and linking its monitor

The same authenticated `workstream-monitor: ` consumer can create a missing
blocker when the request includes an additional `blocker` object containing
exactly `title` and `summary`. This is explicit record approval, not permission
to execute a stage. Example (replace every scope value with the current
canonical private scope; do not use these example paths literally):

```text
workstream-monitor: {"scope":{"profile":"/absolute/profile-home","project":"/absolute/project","audience":"private:12345","root":"12345"},"workstream_id":"ws_0123456789abcdef","blocker":{"title":"CI gate","summary":"Wait for the monitored CI result"},"prompt":"watch CI","schedule":"every 1h","monitor_script":"ci-status.sh","approve":true}
```

The record ID must be `ws_` followed by exactly 16 lowercase hex characters;
no case conversion, whitespace repair, audience collapse or root adoption is
performed. The title/summary must be non-empty, unpadded and within the existing
notepad limits. Recognized secrets are refused before any ledger write; submit
redacted text. A missing record without `blocker` is still refused. Existing
records are not rewritten by the new form: kind, live status, original session,
literal ID, title and summary must match this producer's owned record.

The response confirms a local-only monitor job, not enrollment or linking.
Independently enroll the exact returned job ID in `proactive.eligible_jobs`,
then send a new authenticated message with the identical request (including
`blocker`) plus `job_id`. The job's original session/request and all canonical
scope fields must still match. Existing-record requests without `blocker`
retain the original grammar and linking behavior.

Record creation, provenance and approval CAS use one existing-notepad SQLite
transaction. The creation intent commits before crossing into the existing
jobs.json owner, followed by exact job readback and a saved receipt. Repeating
the exact owned create request returns the same confirmed job. Conflicting
requests or an intent without a confirmed save are denied/unknown, never a
second creation attempt; a partial save needs owner inspection. These are
cross-store failure fences, not a claim of one distributed SQLite/JSON commit.

Creating/approving/linking a blocker does not install a continuation plan or
spend execution authority. Only an original runtime-accepted continuation
stage can be held and resumed; permission intersection, Stop, approval,
revocation, expiry and remaining budget are rechecked at admission. Unchanged
monitor input produces no model call or effect. A source change is evidence,
not inferred authorization. This path is exercised through the real Telegram
private ingress and installed hooks with scripted providers; other surface
coverage belongs to their ingress owners and is not certified here.

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
the isolated Linux scripted-provider tests. The original procedure build
recorded a historical model-substitution exception; this candidate still needs
independent review of its exact final tree before release.

## Verification and helper results (1.5.6 candidate)

A helper return is **result received**, not verified work. Reading or delivering
that output, a zero exit, or a sentence saying “tests passed” does not close an
obligation. The current summary follows the owning result; older failed
attempts stay in evidence and the audit trail. Replays do not create new notes.

Task identity includes the exact source kind, source ID, session and deliverable,
inside the profile/project/audience/root scope. The same words in another job
are not the same work. No migration guesses which old notes belong together;
legacy or unsupported work remains unverified until exact owner proof exists.

### Project checks are reports, not software acceptance

**1.5.6 correction: generic pytest auto-verification is removed.** Even an
explicit human declaration, zero exit, consistent fresh JUnit, and matching
artifact digests cannot prove that required tests ran against those bytes.
Project hooks can fabricate reports, an executable named `python` need not be
Python, and files can change during a check then return to their original
contents. Pinning the interpreter or comparing endpoint hashes is not enough.

The legacy marker `Verify work acceptance acceptance.json` remains a bounded,
authenticated **check-report intake only**. It never resolves an obligation,
creates verified evidence, creates an executed/acceptance receipt, counts toward
procedure learning, or returns acceptance success. A passing-looking report is
stored as `check_report`, status `unverified`, trust `project_report`. The current
summary says the checks **reported** passing and acceptance is unverified.
Earlier failed evidence remains intact. No separate “user accepted” capability
is introduced by this marker.

This applies to every named obligation, including software-only, unknown and
mixed tasks. `delegation` describes transport, not semantic acceptance type.
Human-declared coverage cannot turn “build, review, install and release” into a
software-only criterion. No title keywords classify or close work. An independent
owner with an exact immutable criterion and execution snapshot would be needed
before restoring any generic software acceptance; 1.5.6 provides none.

The existing declaration format is retained for supporting reports only:

```json
{
  "version": 1,
  "coverage": [
    {"workstream_id": "ws_0123456789abcdef", "revision": 2,
     "session_id": "current-exact-session-id", "checks": [0]}
  ],
  "artifacts": {"candidate.zip": "<64 lowercase SHA-256 hex characters>"},
  "checks": [
    {"command": "python -m pytest tests/test_candidate.py --junitxml=acceptance.xml -q",
     "report": "acceptance.xml", "min_tests": 1}
  ]
}
```

In an authenticated private chat, inspect this declaration before sending the
marker. Literal IDs, revisions and session must be current. Contract/artifacts
must already exist; reports must be absent. Every command still uses the normal
foreground terminal and its permissions, with explicit scoped project `workdir`.
The marker launches nothing and grants no permission. The command shape and XML
consistency checks bound admitted data; they do **not** authenticate an
interpreter or attest test execution. Reported test counts are report data.

Limits remain active opt-in mode, exact session/scope, an 8 KiB declaration,
8 obligations, 4 checks, 32 regular local artifacts (4 MiB total), reports up to
1 MiB each, and a 300-second window. Traversal and symlinks are rejected.
Stop/cancel, stale or changed scope/session/revision/evidence, partial checks,
changed declarations/reports/artifact endpoints and concurrent writes fail
closed. Observation identity, original declaration, result row/call IDs and
report digests persist with unverified evidence and current summary atomically.
Exact readback precedes the unverified result. Duplicate replay adds no notes.

### Narrow deterministic audits remain separate

`Verify SHA-256 manifest manifest.sha256` still supports its own runtime-read
file-digest criterion. It proves only that the owner read bytes matching the
predeclared manifest. It does not prove software correctness, review, install,
release or delivery, and it never closes generic delegated obligations. Its
narrow progress evidence and reviewable procedure drafts remain supported.

The Linux tests exercise real local pytest and manifest reads, the authenticated
scripted gateway, temporary SQLite storage, Stop/races and context readback.
They do not prove live provider behavior or Windows/macOS support. This remains
an unapproved local 1.5.6 release candidate: independent exact-commit review,
CI/native-install checks and separately authorized rollout are still required.
No profile or fleet is enabled by preparing it. Historical records are not
rewritten or reconciled by this correction.

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
