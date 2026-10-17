# Proactive workstreams

Telegram's On / Off / Customize controls use existing source admission and
scoped command authority. With an explicit admin list, guests remain read-only;
without an admin gate, admitted users can use the controls. No separate owner
pairing, terminal, enrollment or permission grant is needed. `/proactive status`
explains the current policy. Installation and updates do not enable anything.

Proactive controls reread administrator settings before saving and when a bot
button is used. If an existing managed policy file cannot be read, contains
broken YAML or has a non-mapping root, On, Off and Customize Save do not write
preferences. The bot does not offer new controls until that policy is readable;
an already-open button loses authority. Run `/proactive` again for the reason,
and ask the profile operator to fix the administrator settings before retrying.
Missing managed scope/file, blank or comment-only YAML, YAML `null` and an empty
mapping mean no managed pins; they do not add an owner or admin setup step.
Valid current managed pins and scoped admin restrictions still win. This strict
check is limited to proactive controls; the existing forgiving startup overlay
and unrelated commands are unchanged. It is a fresh pre-write check, not a
transaction with an administrator concurrently replacing the policy file.

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

This capability ships in 1.5.6 but remains opt-in; installing an update does
not enable it or enroll existing jobs. Release gates combine independent
source review, required CI and real native install/update checks. The isolated
Linux scripted-provider tests alone do not certify live providers/transports,
phone/desktop/TUI suggestions or opaque-native compaction. Historical build
records retain their original model identities; release-delta reviews use
GPT-6.1 SOL with explicit provider/model receipts.

## Verification and helper results (1.5.6)

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
Separate native gates exercise fresh installations on Linux, macOS and Windows,
and updates with a running gateway. Windows chat-update proof uses disposable
local IRC, not Telegram. These checks do not certify every live provider or
transport. No profile or fleet is enabled by publishing the update. Historical
records are not rewritten or reconciled by this correction.

### What the live follow-through trial establishes

One representative, owner-authorized HTML task completed build, content/layout
checks, independent SOL review, final checks and delivery without a continuation
nudge. That demonstrates bounded follow-through for that task, not guaranteed
autonomy, idle wake-ups, or comprehensive live Stop/restart recovery. Existing
memory still stores facts and preferences; the optional ledger adds scoped
unfinished-work state and evidence. A side question is not cancellation, but
an explicit Stop overrides pending continuation. Continued work never grants
new permissions.

### Other fixes included in 1.5.6

- Direct answers already written in the current turn survive a helper handoff;
  a question the model never answered still cannot be recovered from nothing.
- A proven-successful council route check no longer wakes another reply after
  the answer was delivered. Failures, new findings and uncertain delivery still
  follow the normal notification path.
- Native Windows work-record reads fail closed on unsafe file objects. Windows
  installs use locked Node dependencies without rewriting tracked lockfiles,
  and update handoffs retain the current owned result through dependency work.
- CLI context-only ingress remains inert. Test isolation, native interpreter
  and source checks, PowerShell/Git layout support, UTF-8 proof logs and confined
  Docker-test permissions strengthen verification without relaxing ownership.

The vanished-folder and distinct automatic-handoff fixes were already released
in 1.5.5 and remain inherited here. Multi-helper raw formatting and split photo
album delivery reports are not claimed fixed by 1.5.6. See `CHANGELOG.md` in the
repository for the release scope and verification boundaries.

## Settings menu

`/proactive` opens a short current-profile menu with effective On/Off state and
an explanation: “Tracks scoped unfinished work through conversation changes.”
When saved and effective settings differ, the short menu says so explicitly.
`/proactive status` shows the technical details (saved/effective state, mode,
reload behavior and authority limits). Telegram shows native **On** /
**Off** buttons for a source-admitted user authorized by the existing scoped
command/control policy. Other
gateway platforms use explicit text
controls unless their adapter supports the same owned-picker guard. Slack uses
`/clover proactive [status|on|off]` (to keep its 50 native-slash slots intact),
or `!proactive`; Matrix also supports `!proactive`. Gateway replies use the
adapter's working typed prefix. CLI and TUI use the same deterministic controls:

| Command | Result |
| --- | --- |
| `/proactive` | Status and controls; no config write |
| `/proactive status` | Read-only saved and effective state |
| `/proactive on` | Enable this profile (`enable` is also accepted) |
| `/proactive off` | Disable only the master flag (`disable` is also accepted) |

Gateway controls use the existing scoped command authorization **and** source
admission. There is no separate owner enrollment, terminal visit, pairing or
code exchange for `/proactive`, including on older profiles without an owner
record. After updating, open `/proactive` and tap **On** to enable it.
Updating or opening the menu never enables proactivity on its own.

- With no admin gate configured for this platform/chat scope, admitted users
  can change proactivity, just as the existing unrestricted command policy
  permits. This does not infer ownership or add anyone to an admin list.
- With a nonempty `platforms.<platform>.allow_admin_from` list, only those admins
  can change settings in DMs. Groups use the independent
  `group_allow_admin_from` policy; DM admin rights do not carry into groups.
  If the group gate is unconfigured, the existing unrestricted group command
  policy applies, subject to group admission and trigger rules.
- Listing `proactive` in a guest's `user_allowed_commands` (or
  `group_user_allowed_commands`) allows read-only status, not settings rights
  when an admin gate is active. Discovery/menu access is not a control grant.
- Source allowlists, pairing/admission rules and managed administrator
  restrictions remain authoritative. Current managed admin restrictions are
  checked again at use, including for an already-open picker. Managed proactive
  settings still reject changes to pinned features/actions.
- `platforms.telegram.verified_owner_controls: false` disables the legacy
  **verified-owner fallback only**, not existing command/control authority.
  That flag is not a global `/proactive` deny switch. Use scoped admin policy
  to restrict who controls settings and managed proactive pins to lock values.

CLI/TUI use the local config-file authority, like `clover config set`. No model
request is needed for any of these controls. A denied menu describes the
current command policy and points to `/proactive status` for permission details;
`/proactive setup` is a read-only alias for those details, not an enrollment flow.
Existing owner-verification records and local pairing commands remain compatible,
but creating, revoking or disabling an owner fallback does not grant or revoke
independent command authority. Other commands and admin lists are unchanged.

### Toggle behavior

The first explicit On on an uncustomized profile selects a useful bounded
package: active work tracking/corrections, work recall, local after-reply
suggestions (3600-second cooldown, at most one per session), and supported
native text-summary auditing. The menu discloses suggestions before the tap.
This does not add model calls, paid watchers, repair, jobs or action authority.

**Saved choices win.** Existing sections (including false, never, zero budgets
and omitted leaves using the old defaults) remain unchanged. Once active,
Off/On only suspends/resumes the master. Fully materialized older default files
cannot be distinguished from intentionally saved Off choices: they are preserved,
not guessed away. Choose **Use recommended → Save → On** to deliberately select
the whole package in that case. Recommended changes only the exposed preset
leaves, sets optional skill drafts Off, and preserves master On/Off, thresholds,
privacy, enrollment, auxiliary budgets and other preferences.

Off sets only `enabled: false`; records remain inspectable. It **does not cancel
independently scheduled jobs or existing loops**, which have their own controls.
An update or opening any menu never saves preferences or starts jobs.

### Customize on a phone

The short native menu uses labelled feature buttons with matching emoji:
Recall, Suggestions, Skill drafts and Context checks. Tap a feature for its
explanation and On/Off choices. Timing chooses after-reply or never; Frequency
chooses hourly or daily cooldown, each capped at one offer per session. Existing
custom cooldown/cap values are displayed and retained unless explicitly changed.
Save commits the draft atomically; Cancel writes nothing. New page tokens prevent
replayed taps from selecting a different control at the same button position.
If another control changes preferences while the draft is open, Save refuses it
instead of overwriting those edits. Unrelated config edits are preserved.

**Advanced / costs** exposes the complete capability inventory without fake
switches: task/job monitoring and authorized continuation guidance; the paid
inbox urgency classifier; model-judge and repair availability; scope, evidence
and limits; routines/reminders; desktop-capture availability. Descriptions and
cost caveats appear after tapping, not as a large Customize front-page paragraph.

The separate [paid inbox watcher](paid-inbox-watcher.md) supports one narrow,
explicit opt-in: connected native Email / INBOX headers, a pinned non-reasoning
OpenAI route, and private native Telegram count alerts. Review source, cadence,
private destination, route, urgency and usage caps, then **Accept extra usage**.
Opening, Cancel, ordinary Save, main On and recommended cannot enable it. A
script-only owned job has no scheduling agent. Durable per-physical-attempt
reservations cover bounded input and generated tokens, not a dollar guarantee.
Discovery never fetches mail or calls a provider. Watcher Off revokes only its
owned job; main Off leaves it independent. Legacy catalog/manual mail jobs and
their costs are unchanged and outside this allowance. Other sources/transports,
multiplexed profiles and desktop/screen/audio capture are unavailable.

Model-assisted suggestion judges are not wired to the foreground offer path;
`max_repairs` does not activate model repair in the supported summary audit.
Their saved limits are visible as information, not misleading working toggles.
Ordinary skill creation and background review/curation remain independent.
Optional procedure drafts reuse `/skills pending`; exact generated-name and
proposal-receipt deduplication exists, but semantic matching to ordinary skills
is not coordinated. Review that overlap; the recommended package leaves drafts Off.

Accessible text equivalents save each valid choice immediately:

```text
/proactive recall on|off
/proactive suggestions on|off
/proactive timing after_reply|never
/proactive frequency hourly|daily
/proactive audit on|off
/proactive skill-drafts on|off
/proactive recommended
```

Read-only discovery: `/proactive customize`, `advanced`, `monitors`, `watcher`,
`judges`, `repairs`, `scope`, `routines`, `desktop`, and `status`. Unknown or
invalid inputs show help without a write. Text `save`/`cancel` are not invented
transactions: use explicit single-choice commands or the native Save/Cancel UI.
See [capability coverage](proactive-controls-coverage.json) for every config gate,
menu entry, consumer and regression test.

The command uses the existing atomic config writer and checks the saved values.
Workstream consumers reload settings at turn/operation boundaries, so **no
restart is required**. In-flight turns keep their current context: the command
does not rebuild cached prompts, swap tools or rewrite message history. Managed
settings may make the effective state differ from the saved state; both appear
in the menu. A failed/unconfirmed write never produces a success reply.

Telegram settings buttons expire after five minutes and bind the original
user, chat, message, thread, adapter and profile. The callback rechecks scoped control
authorization and source admission and is claimed once before applying the change. Old, foreign, revoked
or replayed taps cannot toggle config. If native send fails, use text controls.

### Migration from the old alias

`/proactive` is no longer an alias for `/loop`. For recurring prompts, use
`/loop <interval> <prompt>` and `/loop status | pause | resume | stop`.
Legacy `/proactive <interval> <prompt>` (and other old loop arguments) returns
migration instructions only: it neither runs a prompt nor changes settings.
Existing loop records are not renamed, migrated, cancelled or rescheduled.

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
