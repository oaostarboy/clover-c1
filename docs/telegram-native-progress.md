# Telegram native activity display (`native_progress`)

Opt-in, **off by default**. When enabled for a supported private Telegram chat, the
turn's activity is shown in **one ephemeral native draft** (`sendRichMessageDraft`)
instead of separate progress bubbles: a native `<tg-thinking>` block with the best
available event-grounded action title and elapsed time, the same status / thought /
commentary / tool lines the gateway already shows today, per-tool state (`Running`,
`Done`, `Failed`, `Completed`), optional animated AIActions icons, the partial answer
below it, and a real **Stop** button. A safe tool fallback is used when no meaningful
activity title is present; shell semantics are never guessed.

```yaml
platforms:
  telegram:
    extra:
      rich_messages: true      # required
      native_progress: true    # default false
```

No environment variable. Turning it off restores today's display exactly: the same
`allowed_updates`, no sticker lookup, the same progress bubbles and final delivery.

## What it changes — and what it does not

- **Same content, new presentation.** Every line the current settings show
  (`tool_progress`, `tool_preview_length`, `thinking_progress`, `live_reasoning`,
  `interim_assistant_messages`, `cleanup_progress`) goes through the single composer in
  its original order with its existing redaction. Hidden lines stay hidden; no
  setting is changed; hidden provider reasoning is never enabled or shown.
- **History is kept.** A 30-second draft is not history. On a successful turn
  with `cleanup_progress: true`, the existing collapsed summary is sent after the
  native draft finishes but **before** the separate final answer. The old
  transient tool-progress bubble is not sent, so there is no legacy flash before
  conversion. If the summary send fails, the existing persistent activity-history
  fallback is used; failed, stopped, and genuine-fallback turns keep their prior
  history behavior. The final answer remains the usual separate message, sent
  once, without thinking/emoji markup.
- **It does not touch** `rich_messages`, `rich_drafts`, the final message format or any
  other setting. `native_progress` only lets its own composer use the draft endpoint.

## Activity formatting

A live activity frame uses a specific title from the visible event text when available,
with the selected AIActions role icon and elapsed time. Unknown or underspecified tools
use a safe readable fallback; the renderer does not infer a command's purpose from its
arguments. Public commentary stays in original order as normal text. Thought rows keep one tasteful `💭` marker beside the sentence; exact legacy single-paragraph italics are removed while wording remains intact. Each tool row
keeps one friendly tool label, its permitted detail, and one local status/time. Active
rows read `Running`; successful completion reads `Done`; uncertain completion remains
`Completed`. Supported Markdown emphasis is converted safely; raw code and literal
asterisks are preserved. Completed rows use compact line spacing, not custom styling.

## Availability

Normal private one-to-one chats only, when streaming uses the draft transport, the
bot supports rich messages, **and** the Stop update is subscribed and its authorization
path is available. Anything else — groups, forum topics, private topics, rich messages
off, a draft/capability failure — uses today's display (the feature never shows a
native preview without a working Stop).

## Stop

The Stop button sends `stopped_message_generation` (python-telegram-bot 22.x keeps it
only in `Update.api_kwargs`, so it is subscribed explicitly in `allowed_updates` when the
feature is on). A Stop is honoured only if the draft belongs to a live run of this
gateway, the chat is private with no topic, the user passes the normal Telegram
authorization check, and the run is still the current generation. It then fences the
draft, cancels **only that run** (the existing interrupt hook, with the queued
follow-up preserved and the turn's background processes *not* reaped), keeps the visible
history, and sends one confirmation. Duplicate, stale, foreign, topic, unauthorized or
malformed requests are no-ops. A queued next question is dispatched once as a fresh
turn.

## Budgets (local pacing, not Telegram quotas)

At least 1 s between frames; elapsed refresh every 5 s while a tool runs; idle
keepalive every 15 s (before the official 30 s expiry), at most 15 keepalives or 5
minutes per turn — then everything shown so far is flushed once to the normal progress
message. Final delivery waits at most 2 s for an in-flight draft send; a late accepted
frame cannot revive the draft. Frames are capped at the official 32768-character rich
limit; a larger frame hands the whole activity to the normal progress message instead of
dropping anything.

## Icons

`getStickerSet("AIActions")` is looked up lazily with the adapter's own bot:
single-flight, 5 s timeout, 24 h cache, 5 min negative cache, `RetryAfter` honoured, and
only animation-capable custom-emoji stickers from the returned set are used. Frames never
wait for it; any failure leaves readable text (and, if Telegram rejects custom emoji,
plain sticker emoji). Icon behaviour in real clients and bot entitlement are only
verifiable in a live trial.

## Rollback

Set `native_progress: false` (or remove it) and restart the gateway.
