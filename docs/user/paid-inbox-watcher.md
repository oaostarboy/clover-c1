# Paid inbox watcher (explicit opt-in)

Candidate implementation, not live enrollment or a release approval.

## Supported scope

- One **already-connected native Email adapter**, the configured account and
  `INBOX` only. The new reader uses IMAP `EXAMINE` (`readonly=True`) and bounded
  `BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]<0.4096>` plus UID/INTERNALDATE.
  It does not reuse the gateway's normal RFC822/UNSEEN poller. No body, attachments,
  contacts, calendar, flags, deletion, SMTP, reply or other write operation.
- Fixed `openai/gpt-4o-mini-2024-07-18`, OpenAI API-key authentication and
  `https://api.openai.com/v1`. No auto/task/main route, OAuth recovery, Relay,
  fallback, SDK/transport retries, redirect following or environment proxy.
  Only this non-reasoning route supports the approved output-token ceiling here.
- Native Telegram **private actor's own chat**, no threads, relay or home-channel
  fallback. Delivery is a count-only alert; it cannot identify a message for you.
  Header-only urgency scoring can miss important mail. Check your inbox normally.
- Single-profile native transport only. Multiplexed profiles, standalone cron,
  other mail connectors, other providers/models and group destinations are
  explicitly unavailable. This is inbox monitoring, never desktop surveillance.

Opening discovery reads safe adapter metadata and current policy only. It does
not fetch mail, look up provider credentials or call a provider. A provider key
is checked and fingerprinted only after explicit extra-usage acceptance, without
a provider call. Its fingerprint joins the durable consent. Rotation or removal
denies later fetch/request/delivery and requires a new review; discovery never
looks it up. Authority and credential bindings are rechecked again after scheduler
registration, before an active receipt can be claimed.
No automatic setup/enrollment is performed.

Saved user tool permissions and strict administrator policy also bind consent.
They are read afresh at review/acceptance/registration and before each source
read, physical model attempt and delivery, including queued count-alert retries.
Disabling `cronjob` or removing it from Telegram's saved tool selection prevents
new paid consent and stops later watcher effects; an enabled owned job is not
an irrevocable permission grant. Changed authority invalidates the old consent
rather than expanding it. Invalid or unreadable current authorization fails
closed. Empty/unconfigured permissions keep their normal defaults, and unrelated
user display/turn-limit edits do not invalidate consent. Watcher Off remains
available to the admitted private administrator after a tool denial. These checks
are not an atomic transaction with concurrent external settings writers and
cannot recall a request already sent. They do not change startup config caches.

## Review and enable

In the private Telegram bot, open `/proactive` → Customize → Advanced / costs →
📬 Inbox watcher · Paid, or `/proactive watcher` directly. Choose each field:

1. Exact configured account / INBOX / headers only.
2. Every 30 or 60 minutes.
3. This private chat as the resolved destination.
4. Fixed OpenAI provider/model route.
5. Urgency threshold 7 or 9 for time-critical deadlines, security or service
   interruption. Model input is untrusted data and grants no action authority.
6. Maximum physical attempts: 0 (disabled), 2 or 8 per UTC day. At most 1/run,
   5 items, 4096 input units and 256 generated tokens per attempt.
7. Read the complete cost/scope review, then **Accept extra usage** separately.

Main On, Use recommended, ordinary Save, Cancel, unknown `watcher on` / generic
`watcher accept`, stale/foreign tokens and ordinary catalog acceptance do not
activate this owned watcher. Accept does not save other unsaved Customize choices.

Text parity uses a real memory-only review, not a generic Save transaction:

```text
/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only
```

Read the returned scope/cost review. Its last line contains the exact temporary
`/proactive watcher accept-extra-usage <token>` command. Send that command from
its same actor/private destination/profile within five minutes. It is single-use;
source, policy, credential or route changes invalidate it. Reviews vanish on
restart. Do not copy another chat's token. Zero allowance creates no job.

## Usage contract

These are **bounded usage limits, not a dollar budget or estimate**. Provider
charges may apply. An input unit is one UTF-8 byte of serialized messages plus
128 conservative fixed chat-framing units. This bounds tokenizer input for the
pinned model; it is deliberately more conservative than estimated input tokens.
Per-day input ceiling = chosen daily attempts × 4096. Per-day generated-token
ceiling = chosen daily attempts × 256. The reviewed route has no reasoning-token
expansion. A physical attempt reserves its entire output ceiling, not only the
returned tokens; failed/uncertain requests never refund allowance.

A profile-local SQLite store (`inbox-watcher/state.db`, mode 0600) holds versioned
consent, one owned job ID, UTC allowance/high-water clock, monotonic UID cursor,
sanitized status and pending **count only**. Interprocess `BEGIN IMMEDIATE`
reservation commits immediately before every physical SDK dispatch. Crashes,
concurrent ticks, duplicate ticks and renewed consent cannot reset the current
profile's day allowance. Day advances only forward; backward clocks deny usage.
The physical reservation uses dispatch time, not the tick's pre-fetch timestamp.
Missing, unreadable, malformed or checksum-invalid state denies dispatch. Do not
delete/recreate the allowance to bypass a cap. The checksum detects accidental
state corruption; it is not protection against a local administrator rewriting
both state and checksum or deleting all profile data.

No general scheduled agent runs. The existing scheduler registration owner
persists a typed owned **script-only** job with `no_agent=True`, the shipped
trusted script path, `attach_to_session=False`, no monitor or tools, and local
generic delivery. The script handles its separately resolved private destination
using the live native adapter; the ordinary scheduler cannot redirect private
mail through its fallback routing. Registration and consent are two durable
owners, not a cross-file atomic transaction: pending cannot dispatch; retry finds
the same typed owned job after a registration error/crash and reads it back. Use
`/proactive watcher retry-registration` (or Retry registration on its native
page) to retry only that already-accepted pending snapshot, even after restart.
Changed scope is refused and requires Watcher Off plus a fresh paid review.

The first accepted tick records UIDVALIDITY/UIDNEXT without reading message
headers or classifying history. Later ticks scan at most five new UIDs (holes
included). Server INTERNALDATE enforces a 24-hour lookback. A gap over 24 hours
starts a fresh baseline; UIDVALIDITY changes require fresh review. Large backlogs
can be skipped rather than silently expanding scope. A dispatched batch advances
its cursor atomically with allowance reservation, so uncertain failures or invalid
model output are not automatically rebilled. This deliberately prefers bounded
cost over retrying possibly missed urgent mail.

Only integer scores 0..10, unique known indices and bounded reasons are accepted.
Reasons/headers never enter alert text, receipts, job output or status. Empty,
duplicate and below-threshold data stays quiet. A pending count alert retries at
cadence without another source fetch or model request. Each native PTB send
rechecks authority and needs a positive message ID through `SendResult` before
clearing the outbox. A delivery timeout may have reached Telegram; later retry
can duplicate a count alert, but cannot create a second classification charge.
Operational errors/budget exhaustion produce **no unsolicited notices** (a zero
notification cap); inspect status so a quiet watcher is not mistaken for healthy.

## Status and stop

```text
/proactive watcher status
/proactive watcher off
```

Status shows the persisted allowance day, conservative remaining attempts and last
bounded outcome without mail, even if the mail source disappears. The native
watcher page and main On/Off/status acknowledgements also show existing watcher
state in its authorized private scope, without a source read or provider-key lookup.
Remaining allowance is conservative until the next
physical reservation advances its UTC day. Watcher Off durably revokes consent,
drops pending delivery and pauses only its owned job; it still works if the mail
source disappeared. It cannot unspend a request already transmitted. A new
review/acceptance is needed to enable again; revoked old reviews cannot restart it.

**Main proactivity Off leaves independent jobs running, including this explicitly
consented watcher.** Use Watcher Off separately. Pre-existing manual/catalog jobs
are unchanged, not enrolled and not covered by this budget. Use `/cron` for those.
Do not treat the existing classifier/catalog as bounded by the new allowance.

## Verification boundaries

The candidate exercises Linux/Python 3.11 locked messaging dependencies, native
PTB keyboard serialization and callbacks, real temporary job persistence,
real OpenAI SDK HTTP transport against offline/synthetic endpoints, separate
process allowance races/crashes, and synthetic delivery receipts. No actual
inbox, provider, live enrollment/job, settings, installation or restart was used.
Native Windows/macOS, a real phone layout and real Telegram/provider delivery are
separate release evidence, not implied by offline tests. Current managed-policy
failure denial depends on the separately authored strict M1 policy correction;
this candidate includes that dependency for revalidation, not self-approval.
