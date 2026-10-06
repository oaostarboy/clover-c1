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
  conversion. When no summary is sent (the summary send fails, or the turn failed,
  was stopped, or genuinely fell back) the lines the draft showed are kept as the
  usual persistent activity history. The final answer remains the usual separate
  message, sent once, without thinking/emoji markup.
- **Full diagnostics stay private.** Each turn's full per-call diagnostics (whole
  commands, arguments, exact durations, with the existing redaction) are saved
  once to an owner-only file (`0600`, directory `0700`) under
  `workspace/native-activity/` in the profile home; the newest 200 are kept.
  Nothing is uploaded or announced in the chat: no `activity-details` document,
  no attachment notice, no raw-log fallback. Ask the agent for them when you
  want them.
- **It does not touch** `rich_messages`, `rich_drafts`, the final message format or any
  other setting. `native_progress` only lets its own composer use the draft endpoint.

## Activity formatting

A live activity frame uses a specific title from the visible event text when available,
with the selected AIActions role icon and elapsed time. Unknown or underspecified tools
use a safe readable fallback; the renderer does not infer a command's purpose from its
arguments. Public commentary stays in original order; only the newest update is bold, earlier ones keep their words at normal weight. Thought rows keep one tasteful `💭` marker beside the sentence; exact legacy single-paragraph italics are removed while wording remains intact. Each tool row
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
keepalive every 15 s (before the official 30 s expiry) for as long as the turn runs.
While an answer is streaming the activity block is held still: its header and row
timers stop ticking, so each frame differs only by the appended answer text; a tool
boundary returns it to the live clock.
Final delivery waits at most 2 s for an in-flight draft send; a late accepted
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
