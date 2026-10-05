# Telegram native activity display (`native_progress`)

Opt-in, **off by default**. When enabled for a supported private Telegram chat, the
turn's activity is shown in **one ephemeral native draft** (`sendRichMessageDraft`)
instead of separate progress bubbles: a native `<tg-thinking>` block with a timed
header (`Searching · 12s — …`), the same status / thought / commentary / tool lines
the gateway already shows today, per-tool state (`Executing`, `Succeeded`, `Failed`,
`Completed`), optional animated AIActions icons, the partial answer below it, and a
real **Stop** button.

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
- **History is kept.** A 30-second draft is not history, so at turn end the visible
  lines are persisted once through the existing progress path *before* the final reply:
  with `cleanup_progress: true` that artifact is collapsed into the usual summary card;
  otherwise (and on failure or Stop) it stays as the progress message. The final answer
  is the usual separate message, sent once, with no thinking/emoji markup.
- **It does not touch** `rich_messages`, `rich_drafts`, the final message format or any
  other setting. `native_progress` only lets its own composer use the draft endpoint.

## Where it is active

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
