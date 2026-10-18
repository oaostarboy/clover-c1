# W3 presence delivery/control boundary — provisional candidate

This packet begins W3 under the NEKO continuity plan. It is not S3 acceptance.
The coordinator has not supplied a frozen W1/W2 API or transferred integration
ownership. The DAG therefore permits only disjoint surface/data-contract work
now. Nothing here is imported by a live gateway, enabled, or deployed.

## Boundary to review before integration

- `gateway/presence_delivery.py` provides pure publication preparation plus an
  ephemeral, bounded menu/request projection. It never creates a synthetic user
  turn, prompt edit, send or delivery receipt. It consumes host-verified snapshots,
  not client authority. It performs no filesystem, DB, config or provider I/O.
- A publication binds an exact workstream Scope plus conversation root, thought
  ID/revision, event, profile initiative epoch and generation. All are literal;
  no scope repair, principal migration, env-based surface detection or automatic
  linking is allowed.
- A destination is adapter/credential-instance/principal/conversation bound.
  Telegram is private-DM-only. Cross-surface mapping requires a current explicit
  authenticated identity link, even if principal IDs have the same digits.
- Preparation and final validation use the same policy checks. Default-off,
  master proactive/suggestion/managed denial, quiet/busy/stale evidence, expiry,
  Stop epoch/new-input generation changes all hold. Prepared is not authorized,
  claimed, attempted or delivered.
- Controls render Inspect, Correct, Forget and Stop everywhere. Stop means all
  presence initiative in this profile, not unrelated jobs or requested replies.
  Mutations yield typed requests only; host authorization, managed policy, source
  owner CAS and epoch mutation remain with their existing/future authorities.
  Disabled records remain inspectable/deletable. There is no On action here.
- Control callbacks carry random server-issued opaque tokens, never IDs,
  correction text or authorization assertions. A menu binding includes profile,
  actor, adapter/credential instance, conversation/message/thread, scope, epoch,
  expiry and single-use replay protection. The host must bind to an ACKed native
  message before attaching controls; a missing message ACK must not activate
  buttons. The client never supplies authority.
- Desktop consumes plain JSON, never HTML/model-rendered actions; publishing
  does not select a session, focus a window or inject chat history. A feed
  acceptance ACK is not a user-read receipt. Telegram ACK does not mean read.

## Implemented provisional API

- `prepare_publication(DeliveryOffer, PublicationContext) -> Preparation` holds
  by default. Scope values are non-empty/unpadded literal strings; this projection
  currently requires an explicit `private:<principal>` audience and conversation
  root. W1's native thought/source objects are not imported or migrated here.
- `surface_payload(PreparedPublication, fresh_context) -> dict | None` repeats
  checks and pins the exact captured adapter binding. Telegram output is native
  plain text, links do not unfurl, and notification is explicitly silent. Desktop
  output is `presence.initiative` v1 with correlation fields, no history roles,
  and explicit `focus: false` / `navigate: false`.
- `PresenceControls.open_menu(ControlContext)` returns JSON with fixed action IDs
  (`inspect`, `begin_correction`, `forget`, `stop`) and 128-bit random callbacks.
  Native message ACK identity is required first; the existing message can then be
  edited to attach the buttons. Tokens last 300 seconds, superseded same-context
  menus are invalidated, and at most 256 tickets remain per instance. Restart
  loses tickets. This ephemeral cache is NOT the durable cross-surface arbiter.
- `PresenceControls.request(token, fresh_context)` rechecks exact scope,
  adapter/credential instance, principal, actor, conversation/message/thread,
  epoch, thought/revision, current permissions and managed readability/action
  allowance. Invalid foreign taps do not consume the legitimate token. Successful
  consumption yields one `ControlRequest`, not a save/Stop/correction receipt.
- A Stop request contains only profile + expected epoch (no thought/project
  target). The future profile epoch owner must apply it across every linked
  surface and cancel pending initiative. No epoch is incremented here.
- Correct starts a correction interaction; this packet does not accept or save
  free-text corrections. The next authenticated human row must go through W1's
  source/correction contract. No source inspection/deletion store is implemented.
- `telegram_control_markup(menu)` is stdlib-only and returns PTB-compatible native
  keyboard data. Desktop should localize the fixed action IDs in all four locales,
  not render arbitrary model-supplied actions. Backend English labels are fallback
  presentation data only; neither labels nor client JSON grant authority.

`PublicationContext` / `ControlContext` are trusted-owner projections, NOT HTTP
request bodies, credential objects or proof of authentication. In particular,
`identity_link` is an opaque handle from the future authenticated identity-link
owner; a non-empty string is not independently verified here. The host must
re-read current policy/identity/evidence after awaits and atomically fence the
final send with the future claim/epoch owner. A check followed by a send is not
an atomic Stop guarantee. There is no timer, scheduler or automatic fallback.

## Pending coordinator checkpoints (must not be bypassed)

1. Freeze W1 thought/source revision, deletion/correction invalidation and epoch
   owner; freeze W2 proposal/event/generation output. Map them to this boundary
   or revise the provisional contract before any host wiring.
2. Pick the authoritative transactional claim/receipt store with W1. Claim
   thought+revision with epoch, lease owner and CAS across both surfaces. Preserve
   proposed/claimed/attempting/acknowledged/failed/unknown distinction. Mid-send
   crash or missing native ACK holds as unknown without automatic resend. Pure
   preparation here does not implement that durable arbiter.
3. Transfer `gateway/run.py`, effective config, `clover_cli/proactive.py` and
   Telegram handler ownership. Reuse existing command/source admission and fresh
   strict managed policy reads; do not restore owner-enrollment ceremony.
4. Desktop specialist wires actual backend status-feed producer and controls in
   `apps/desktop/src/app/chat/`, not just composer presence. Require current
   backend capability and authenticated profile/source routing.
5. Exercise genuine Telegram ingress + callback + native HTTP ACK, desktop
   feed/control producer, Stop races, restart/claims, combined candidate tests,
   independent review, packaged notices and native OS gates before release.

This is original Clover boundary code. No additional NEKO code, dependencies,
assets, weights or licenses are copied. NEKO delivery ownership/TTL is a design
reference only (pinned source and attribution map remain in the parent plan).
