# Presence integration candidate (not release approval)

This isolated candidate joins S0/W1/W2/W3 and repairs the reproduced W4 defects.
It remains **default off**. It does not authorize deployment, restart, a version
change, paid inference, live configuration changes or real message sends.

## Connected application paths

Private authenticated Telegram and server-authenticated desktop WebSocket input
reach the existing receipt-backed `prepare_offer_turn` seam. `Carry thought:
<summary>` admits a durable W1 thought; only the first line is its summary. The
complete, nominated human row remains evidence. No historical scan is performed.

A subsequent relevant, successful requested reply must receive its real surface
ACK before W2 can assess an optional initiative. Telegram uses the adapter's
existing generation-bound post-delivery callback. Desktop stages an opaque,
transport/principal/session-bound capability on `message.complete`; the actual
renderer acknowledges the requested reply after storing it. Neither path inserts
an invented user message or turns the initiative into an ordinary reply footer.

W2 reads bounded W1 state and shared profile-local history in `cron/presence.db`.
Before inference it reserves at most one physical attempt per event and thought
revision, three calls/day/profile, and 512 requested output tokens, further
bounded by existing proactive caps. Failed calls, refusals and timeouts consume
reservations. No refunds, generic retries, provider fallbacks, tools, sensors,
background research or guessed cost accounting are added.

The auxiliary route is `agent.auxiliary_client.presence_completion`, not the
general retrying call helper. Only explicit OpenAI-compatible API-key routes
(`auxiliary.presence.provider: openai` or `custom`, plus an explicit model) are
supported. Custom also requires its explicit `base_url`; missing endpoints hold
before auto-detection. OAuth, auto-selection and compatibility wrappers hold.
Credentials remain in the existing credential owner. The native async SDK uses
`max_retries=0`, redirects disabled, a bounded timeout and cancellation. Ordinary provider calls are
unchanged.

W3 validates, CAS-claims and attempts a distinct publication through the surface
owner. Telegram makes one plain `sendMessage` attempt without Markdown fallback,
splitting, delivery-ledger retry or notification escalation. Desktop emits a
bounded `presence.initiative` status-feed frame with `focus: false` and
`navigate: false`. The mounted renderer stores a profile/session-scoped row and
round-trips its opaque ACK to that exact authenticated owner. It never inserts a
chat bubble, navigates or takes focus. Its card offers Inspect, Forget, Correct
and Stop. Labels exist in all shipped desktop locales.

An ACK marks the durable receipt acknowledged only while source/policy/generation
remain current. Cancellation, revoked state or a missing/ambiguous ACK holds.
Attempting/claimed/unknown records never replay automatically, including after
restart or a process ending between durable attempt and ACK. There is no
exactly-once delivery or atomic cross-database/network guarantee.

## Explicit cross-surface sharing

Unlinked conversations do not share thoughts. A numeric user ID, matching project
name, client `principal_id` or display name is never an identity link.

1. In an authenticated private Telegram conversation, use `/proactive presence
   link` under the existing scoped admin and managed-policy gates.
2. In your authenticated desktop conversation in the **same profile and exact
   project**, use `/proactive presence link <code> confirm` within five minutes.
   Both conversations must first have a real requested turn.
3. Both authenticated owners now refer to one canonical presence context. Only
   future explicitly carried thoughts enter it; no old thoughts/history migrate.
4. `/proactive presence unlink` from either linked conversation revokes the link
   and advances the shared epoch. Re-linking creates a new context, not a replay.

The one-use challenge is stored only as a hash. The existing W1 store owns link
membership, budget, proposals, claims, generation and revocation; there is no
second scheduler, identity authority or publication database. Exact source-row
reads additionally verify the persisted session owner/surface and digest.

## Explicit project return

`presence.project_return_enabled` is a separate **false-by-default** narrowing
control. Global presence/proactive/managed/attention policy must still allow it.
A successful authenticated desktop reply can advertise this capability to its
renderer. The actual composer status stack sends one `presence.project_return`
opportunity when the user selects that backend-enabled conversation; no timer,
visibility poll or history scan runs. An ordinary selected conversation with no
backend capability sends no presence RPC.

The server verifies the exact current session, login identity object, transport,
profile/project, idle state and retained authenticated context anchor. It creates
an opportunity, not a human-ingress receipt or user utterance. Its topic is the
last nominated visible human topic, not a manufactured instruction. It advances
the shared generation, refreshes the current linked context and uses the same
W1/W2/W3 path. Quiet hours, relevance, caps, source validity, Stop and no-replay
rules still apply. After a server restart a fresh authenticated requested turn
must establish an anchor; no unsolicited history recovery is attempted.

## Current-turn emotional context

Only current authenticated bounded input containing explicit `frustrated` or
`frustrating` language is screened. This narrow producer is not a general emotion
detector or accuracy evaluation. It shares the same profile/day/event budget.
W1 validates optional VA/confidence, exact evidence, expiry and generation.
Invalid JSON/numerics, low confidence, Stop, deletion/correction, invalid managed
policy or provider failure produce ordinary composition.

Careful/brief posture reaches only the current real user message's `api_content`
sidecar before the ordinary request. Raw user content, cached system prefix,
previous messages and tools do not change. Exact sent bytes persist through an
expected-row guard for cache-stable replay. Raw affect samples/history are not
stored. `Presence: ordinary` corrects the next current turn. No affect changes
permissions, tools, budget, obligations or authority. This path is exercised on
both Telegram and desktop. MoA/Codex app-server are not enabled by this candidate.

## Controls, rollback and storage

Native `/proactive presence status | stop | resume | forget <id> | correct <id> |
link | unlink` controls use existing authenticated surface ownership and managed
policy. Desktop link confirmation uses the syntax above. Status is a dormant
exact-context inspection. Forget/correct tombstones the derivative, not the
original session message, and stops pending initiative. Resume admits future
thoughts only; it does not enable disabled settings, refund budget or replay
unknown sends. Scoped admin authorization still applies on Telegram.

Generic Telegram `/stop` and authenticated desktop `session.interrupt` now revoke
the shared profile presence epoch as well as performing their existing ordinary
Stop behavior. Normal configuration OFF writes revoke before saving; OFF->ON
resets old pending records before enabling. A corrupt presence DB remains
untouched and fails closed, but does not prevent saving OFF. Disabling optional
presence does not disable requested replies.

Optional `presence.quiet_hours: {start_utc: 22, end_utc: 7}` gates inference and
publication; malformed schedules hold. `presence.denied_categories` can veto W1
purposes. The explicit proactive after-reply timing preference remains mandatory.

Schema 4 additively extends candidate schemas 1/2/3 on authorized writes, never on
reads. Unknown/corrupt schemas hold without repair. Human input generation is
captured at the authenticated mint, including queued input, and sealed into the
existing ingress capability. Independent processes share its SQLite counter.
Proposal/claim/attempt/ACK transactions compare generation and epoch under the
existing SQLite write lock. Link changes and Stop invalidate old work.

Thought, tombstone and owner tables retain at most 128 records; identity tables
have separate 32-row caps, with aggregate logical payload bounds. Expired link
challenges can be removed on a new issue. No unsafe publication lease stealing or
automatic reservation/tombstone eviction is implemented. Exhaustion holds until
an explicit reviewed retention/reset design exists.

## Evidence and remaining release gates

Canonical Python tests use temporary homes and a network guard. Native SDK HTTP,
Telegram Bot and server login boundaries are synthetic; no live inference or send
occurs. A connected offline bridge sends an actual Python application frame to
the mounted TypeScript composer/status renderer, then dispatches that renderer's
ACK to the still-live owner and checks its durable acknowledged receipt. This is
not a static JSON fixture or an Electron installer/native-window acceptance test.

The desktop production renderer/Electron bundles build on Linux. Normal Python
wheel/sdist creation is deliberately unsupported by the project's existing
installer/Docker/Nix distribution guard; it was not bypassed by pretending to be
a Nix build. NEKO LICENSE, NOTICE and provenance declarations remain packaged in
the source archive and license-file metadata.

Still required before release: independent merged specification/security review
and fresh adversarial review; full repository/platform acceptance beyond the
recorded selected regression set; Windows/macOS and actual native desktop runtime
acceptance; streaming-final reply ACK, MoA/Codex and provider compatibility gates;
explicit multiplex-profile acceptance; reviewed bounded retention/reset UX;
canonical SessionDB mutation versus transport crash/race acceptance; emotional
accuracy/user evaluation; actual Nix/Docker/native-package NOTICE audits and
product migration/rollback tests. No Linux result implies those gates passed.

The source-pinned handoff and raw receipts are authoritative for the exact tested
SHA and counts. Nothing was pushed, deployed, restarted or enabled in a live
profile. Installed skills, prompts and user memories were not changed.
