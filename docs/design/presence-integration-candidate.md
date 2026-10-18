# Presence integration candidate (not release approval)

This isolated candidate joins the S0/W1/W2/W3 packets. It remains default off.
It does not authorize deployment, restart, version changes, or real provider use.

## Connected route

Private authenticated Telegram input reaches the existing receipt-backed
`prepare_offer_turn` seam. `Carry thought: <summary>` admits a durable W1 thought.
Only the first line is the summary. The complete nominated human row remains
source evidence; there is no historical scan. A subsequent relevant, successful
requested text reply must receive the adapter's actual send ACK before the
existing post-delivery callback can run W2. No timer is installed.

W2 projects W1 state plus profile-local proposal history from the same
`cron/presence.db`. It reserves before dispatch: at most one attempt per event
and thought revision, three/day/profile, and 512 requested output tokens, further
bounded by existing proactive auxiliary caps. Failed calls, refusals and timeouts
consume reservations. There are no refunds, retries, provider fallbacks, tools,
research or guessed currency. The physical auxiliary request is through
`agent.auxiliary_client.presence_completion`, not `async_call_llm`.

The bounded auxiliary candidate accepts **explicit OpenAI-compatible API-key
routes only** (`auxiliary.presence.provider: openai` or `custom`, plus an explicit
model). OAuth, auto-selection and compatibility wrappers hold. Credentials stay
in the existing credential owner, never in presence state/config. The native
async SDK receives `max_retries=0`; cancellation propagates. The normal agent's
provider and its retry behavior are unchanged.

W3 validates the proposal, then the same profile store CAS-claims it. The
Telegram adapter owns a distinct plain-text `sendMessage` attempt. This is not
an inline footer and not an invented user utterance. No initiative is inserted
into provider history. An adapter ACK transitions to acknowledged; exceptions,
cancellation, missing ACK or revoked state hold as unknown. Attempting/claimed
records are never automatically replayed, including after restart. An unresolved
attempting record is ambiguous, not an ACK. No exactly-once guarantee is claimed.

## Current-turn posture

Only a current authenticated bounded human input containing explicit
`frustrated`/`frustrating` language is screened for tentative affect. This is a
narrow first assessment producer, not a general emotion detector. Its request
shares the same profile/day/event budget. W1 validates the optional VA/confidence
reading, evidence and generation. Low confidence, invalid JSON/numerics, expiry,
Stop, source deletion/correction, invalid managed policy or provider failure
produces ordinary composition.

A careful/brief posture is appended only to the current real user message's
`api_content` sidecar, before the ordinary provider request. Raw user content,
cached system prefix, previous messages and tools do not change. Exact sent
sidecar bytes are persisted to keep later replay cache-stable; raw emotional
samples/history are not stored. `Presence: ordinary` is a current-turn correction;
a following ordinary reply has no posture injection. No affect grants authority.
MoA and Codex app-server composition are not enabled by this candidate.

## Controls and settings

In a development profile, existing configuration commands can set
`presence.enabled`; this is independent of and subordinate to proactive master,
active mode, suggestions, managed policy, and the explicit after-reply timing
preference. No live profile was changed by this implementation.

Private Telegram controls use the existing `/proactive` command's current scoped
admin authorization, source admission, stale-command and managed-policy gates:

- `/proactive presence status`: exact-conversation dormant thought inspection.
- `/proactive presence stop`: durable profile-wide initiative epoch revocation;
  ordinary requested replies remain available.
- `/proactive presence forget <exact-id>`: tombstone the source-derived thought
  and stop pending initiative. It does not delete the original session message.
- `/proactive presence correct <exact-id>`: invalidate the old thought and stop;
  a new authenticated `Carry thought:` input can supply a replacement.
- `/proactive presence resume`: explicit reset for future thoughts only. It
  invalidates old pending thoughts and does not enable any disabled settings,
  replay unknown sends, or refund provider budget.

Optional `presence.quiet_hours: {start_utc: 22, end_utc: 7}` gates opportunities
and affect inference. Values are integer UTC hours 0..24; malformed schedules
hold, equal endpoints mean all day. No schedule means no additional quiet window;
the existing explicit after-reply attention preference remains mandatory for
initiative. `presence.denied_categories` can veto W1 thought purposes.

## State and rollback

Schema 2 adds epoch/budget/publication tables to the existing W1 presence store.
Schema 1 migrates additively on an authorized write, never on a read. Unknown or
corrupt schemas hold. Tables retain at most 128 rows; logical thought/receipt
payload is bounded. No automatic tombstone or reservation eviction is implemented;
capacity exhaustion holds and needs an explicit reviewed retention/reset design.

Disabling effective policy stops provider/context/publication at each fence.
Use the explicit Stop control for a durable epoch revocation, then disable the
setting for rollback. Generic config off/on transitions do not yet have a
profile-wide mutation hook, so automatic rollback/re-enable acceptance remains a
release blocker. Dormant inspect/forget stays available under authorization.

## Exact remaining release work

- Desktop: W3 payload/control contracts are integrated, but no authenticated
  desktop initiative producer, renderer/status-feed consumer, transport ACK or
  native controls are wired. No desktop speak-first claim. Explicit identity
  linkage across Telegram/desktop must be implemented; numeric IDs are not links.
- Actual project-return event producer is not wired; only after-reply is live in
  the candidate. No general automatic thought extraction or general affect model.
- Existing `/stop` interrupts its ordinary session/generation, but is not yet
  bound to the new profile-wide epoch. Use `/proactive presence stop` for the
  durable presence Stop in this candidate. Cross-surface correction/input needs
  linkage and a shared durable generation owner, not the current gateway run map.
- Readback fences surround provider, proposal, claim and native publication, but
  SessionDB source mutation, presence transactions and physical transport are not
  one atomic transaction. Independent cross-process source/Stop/publish race and
  crash/lease recovery acceptance remains required. There is no automatic resend.
- Multiplex-profile callback-context acceptance and a bounded authorized retention
  reset remain required before enabling beyond the tested single-profile route.
- Supported provider role serialization outside the tested native chat route,
  streaming-final ACK handoff, MoA/Codex app-server parity and emotional accuracy
  remain unproven. Streaming-only requested responses conservatively do not offer.
- Full repository suite, independent merged specification/security review and
  fresh W4 recheck, native Windows/macOS/Linux, migration/rollback product tests,
  Nix/Docker built-artifact license/NOTICE audit and native desktop acceptance are
  release gates. Linux targeted tests do not imply any of these gates passed.

No source was copied into a running release. No paid inference or external send
was performed. Test providers use native SDK requests against synthetic HTTP
transports; application networking is blocked. Test Telegram sends end at a
scripted native Bot boundary. See the integration handoff for source-pinned logs.
