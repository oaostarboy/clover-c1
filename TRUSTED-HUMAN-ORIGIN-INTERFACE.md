# Trusted human-origin receipt interface

Status: contract published; implementation intentionally unavailable until the real gateway event identity is propagated. Consumers MUST fail closed while `SessionDB.get_human_origin` is absent or returns `None`.

## Read API

`SessionDB.get_human_origin(session_id: str, message_row_id: int) -> Optional[agent.workstreams.HumanOrigin]`

The returned value is `HumanOrigin(profile, session_id, message_row_id, ingress_event_id, origin="human", consumed=False)`. The lookup is scoped to the active profile's SessionDB and must match the exact durable message row. It returns `None` for a missing, stale, replayed, profile-mismatched, consumed, non-human, synthetic, or untrusted-ingress row. It MUST NOT infer provenance from the row's `role`, content, display metadata, platform name, terminal state, or a caller-supplied row/event ID.

Adapter matching Sonnet's `HumanOriginReader` callable contract:

`read_human_origin(db, session_id, message_row_id) -> Optional[HumanOrigin]`

Implementation: return `db.get_human_origin(session_id, message_row_id)`; do not synthesize or normalize receipt fields. Pass this reader to `workstreams.verified_human_origin(origin, db=session_db, human_reader=read_human_origin)`. Consumers must compare the read-back object to the exact candidate and continue enforcing current-profile and unconsumed checks.

## Write-side invariant

A receipt is valid only when inserted atomically with the exact human message row, or durably linked by its exact SQLite row ID in the same profile database. `ingress_event_id` must originate from a real authenticated user event at the gateway boundary, not from `run_conversation` text, a synthetic role-user row, helper completion, model/tool output, TTY/PTY, or an untrusted API caller. Event IDs are opaque; no content hash or guessed message ID is a substitute. If the gateway cannot provide this provenance, no receipt is written.

The currently inspected `gateway/run.py` call at `gateway/run.py:6965-6995` passes only the model-facing message, conversation history, session task ID, and persistence overrides to `AIAgent.run_conversation`. It does not pass `ctx.event_message_id` or an ingress-origin capability. The only direct means of binding a trustworthy event to the row therefore requires a separately authorized ingress pass-through at that seam. Until its owner is explicitly assigned, this document does not claim that the write side or `SessionDB.get_human_origin` exists.

## Disabled behavior

When `proactive.enabled` is false, do not create a receipt row, workstream row, or marker table entry; do not change transcript content or API context. Existing SessionDB transcript persistence is unchanged. When enabled but ingress provenance is missing or ambiguous, persist the ordinary transcript as before and leave origin unknown.
