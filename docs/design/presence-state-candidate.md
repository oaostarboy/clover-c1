# W1: presence state candidate (not enabled or integrated)

## Scope and ownership

Implements the W1 packet in the NEKO continuity plan. This is a library candidate,
not a complete emotional engine or release. No gateway, prompt, config default,
provider, scheduler, delivery, persona, permission, skill or memory-provider path
imports it. W0's inherited shadow slice remains unchanged. All tests use temporary
homes and synthetic assessment output; source readback tests also exercise real
SessionDB human receipts and real persisted row corrections/deletion.

The durable part is **pending thoughts and invalidation metadata**, not emotional
history. Affect is one tentative instantaneous reading. It disappears after a
restart. There are no diagnoses, attachment scores or personality updates.

## Typed interface handed to W0/W2/W3

`agent.presence_state` publishes:

- `ContextKey(scope: agent.workstreams.Scope, conversation_root: str)`. All five
  fields match literally. Identifiers are bounded and never normalized/repaired.
- `EvidenceRef(owner, source_id, revision, digest)`. Revision is a positive integer
  (not bool); digest is exact lowercase SHA-256. Owner names identify the real
  storage subsystem, not a URL-shaped assertion.
- `EvidenceSnapshot(key, ref, trusted)` and `EvidenceReader(key, ref) -> snapshot |
  None`. **The authenticated host supplies this reader.** It must authenticate the
  source's actual owner, human provenance, current revision/content and exact
  scope, including conversation root. Model output cannot create trusted evidence.
- `Thought`: immutable key/source, thought_id, record revision, purpose, bounded
  summary, status, created/updated/expires UTC timestamps.
- `ThoughtRepository` protocol, implemented by `cron.presence.ThoughtStore`.
  Its read-only `source_is_current(key, ref)` checks durable source high-water
  marks/tombstones without creating a database. ThoughtState's affect reader uses
  it so feedback from another owner/process also revokes existing readings and
  future old-source assessments, even if the original message remains unchanged.
- `ThoughtState(key, reader, store, clock=..., enabled=False)`:
  - `admit(ref, redacted_summary, purpose='follow_up', ttl=one_hour) -> Thought |
    None`. The host must redact before admission. Source readback is mandatory.
  - `list_pending() -> tuple[Thought, ...]`. Reads validate scope, TTL, source
    provenance and durable revisions. Expired/invalid sources become tombstones.
  - `inspect() -> tuple[Thought, ...]`. Read-only, exact-scope metadata, including
    dormant tombstones; available when disabled. Caller must authorize inspection.
  - `invalidate_source(ref, status='corrected' | 'deleted' | 'invalidated') -> int`.
    Invalidates all earlier/equal source revisions in this exact context, wipes
    their summaries, increments their record revisions and resets local affect.
    Persistent source tombstones also block late admission when no thought had
    yet committed. Zero can mean no matching thoughts OR failure: inspect
    `last_error`; do not report successful deletion on the count alone.
  - `is_current(thought) -> bool`. Final revision/source/TTL readback only. **Not
    authorization, a claim, epoch check, consent, or a delivery receipt.**
  - `set_enabled(bool)`. Revokes captured affect work and suppresses new
    admission/active reads. No automatic producer exists to replay old records.
  - `affect: AffectState`; `last_error` signals storage failure/capacity holds.

Allowed thought purposes are follow_up, project_connection and shared_language.
Shared_language is only a purpose tag: a separate confirmed shared-context owner
and its confirmation controls are not implemented here.

`AffectState` publishes `begin(ref) -> AssessmentTicket | None`,
`commit(ticket, sample) -> bool`, `correct()`, `set_enabled(bool)`, `latest` and
`posture`. A ticket captures generation and observation time **before inference**;
it is consumed once. New input, correction, revocation or a newer begin rejects
older completions. Caller must call `correct()` when new input arrives even if no
new assessment is attempted. The core performs no inference or budget reservation.
W2 owns those pre-invocation gates, event IDs, policy epoch, budget and deadline.

An assessment contains exactly numeric, finite valence [-1,1], arousal [0,1] and
confidence [0,1]. No bool, string coercion, partial fields, extra diagnostic labels,
NaN or infinity. A reading expires at 120 seconds from ticket observation time;
slow completion cannot refresh it. Clock rollback yields ordinary posture. Source
readback failure or correction also clears it. Confidence below 0.7 is ordinary;
confident valence below -0.3 gives careful_brief. No posture expands authority.
Direct AffectState calls are synchronous, conversation-owned operations, not a
cross-process state or a background classification loop.

## Store, migration and invalidation

Choose a **separately versioned profile DB**:
`get_clover_home()/cron/presence.db`, resolved at each operation, not import time.
No changes to the existing notepad/workstream/SessionDB schemas, jobs, obligations
or private SQL helpers. Reuses notepad's connection-per-operation and busy-timeout
pattern. SQLite rollback-journal transactions deliberately avoid changing journal
mode or attaching tables to an existing owner. `BEGIN IMMEDIATE` covers schema
migration, duplicate checks, capacity, source supersession and mutation, including
independent connection contenders. Newly created files are owner-only on POSIX.

Schema 1 has presence_thoughts and presence_invalidations. The sole migration is
an actually empty v0 DB to schema 1; there is no deployed legacy affect import.
Newer versions, nonempty unknown v0, corrupt SQLite, malformed JSON, invalid
semantic fields or mismatched indexed/payload provenance fail closed. Existing
unknown/corrupt files are not repaired or overwritten. Service calls hold with
`last_error='store_unavailable'`; low-level repository calls raise
`PresenceStoreError`. Native Windows/macOS SQLite behavior remains a release gate.

Duplicate source revision/purpose returns one stable identity without refreshing
its expiry. A higher source revision atomically supersedes older derivatives.
Source correction/deletion tombstones survive restart and block old/late results.
Default thought TTL is one hour, maximum seven days, strict at the expiry boundary.
All invalidations remove the logical summary; **not a secure-erase guarantee** for
SQLite pages, journals, backups or the original source owner.

Caps: 128 thought rows and 128 source-invalidation rows per profile; 64 KiB total
logical serialized payload. Terminal rows consume capacity; they are not silently
evicted to make old sources replayable. Capacity is checked in the transaction;
excess admissions roll back and hold. User-authorized pruning/reset with safe
source retention semantics remains a coordinator/control-owner design task, not
an implicit retention policy hidden in this library.

## Integration obligations / remaining gates

- W0 must provide production receipt-backed evidence adapters and replace its
  ephemeral shadow storage at its owned seam. Its `PendingThought`/AffectReading
  shapes differ; no implicit conversion is installed by this packet.
- W2 must dedupe old source events, reserve budget before inference, bind
  generation/initiative epoch and avoid reassessing corrected old utterances.
  Automatic extraction/inference, current-turn posture composition and useful
  connection generation are not built here.
- W3 must enforce effective proactive/presence/managed policy and authenticated
  controls, bind profile/context on every turn, fan out source-owner corrections
  to affected context owners, and revalidate captured records before publishing.
  Cross-surface claims, global Stop, leases, ACK/unknown handling and adapter queues
  remain W3-owned. There is no atomic transaction with the source DB or transport;
  `is_current()` must not be described as an exactly-once publication guarantee.
- W4/coordinator must run independent review, combined/source-pinned regressions,
  full repository/native OS/provider serialization gates and packaging audit.

## NEKO adaptation and license handoff

Pinned source: Project N.E.K.O. `8d767935f99c31b9b62b9c360d5c181bd48a7d16`,
`main_logic/activity/master_emotion.py:62-102,190-281`. The bounded reading shape,
latest/TTL and sequence/reset mechanism inform `agent/presence_state.py`'s
AffectReading/AffectState. Retained Apache-2.0 header and prominent modification
notice are in that module; verbatim upstream LICENSE/NOTICE are inherited under
third_party/neko/. Strict validation, scoped source readback, single-use tickets,
durable thoughts and repository are Clover design. No NEKO models, data, fonts,
assets or dependencies are imported. W0 owns the shared provenance manifest: add
this adapted location there during integration. Packaged LICENSE/NOTICE inclusion
is still a coordinator/release gate, not established by unit tests.
