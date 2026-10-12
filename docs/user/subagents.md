# How Clover hands work to subagents

Clover can do a job itself, or hand it to subagents (helpers that work in the
background while you keep chatting). It decides this for every message you send.

## How it decides

- **A small, fast model makes the call.** Before your main model starts, a small
  model reads your message and picks: do it here, or hand it off. Clover chooses
  one from your AI company on its own (for example GPT-6 Luna, Claude Haiku or
  Gemini Flash). If your company has no small model, Clover uses your main model
  and waits up to 20 seconds for its answer.
- **It thinks a little first.** With thinking off, small models often call a big
  job "quick". So most small models get a short thinking step before they
  answer. Claude Haiku 4.5 is the exception: it answers without thinking,
  because thinking made it slower and no more accurate in our tests.
- **Small talk is free.** "Thanks", "ok" and "lol" skip the decision, so no
  tokens are spent on them.
- **Your words always win.** Say "use subagents" and Clover hands it off, even
  for a small job. Say "do it yourself" and Clover stays in the chat, even for a
  big job.
- **A safety net catches wrong guesses.** If a job Clover called "quick" is still
  running after 25 tool calls or 3 minutes, the rest is handed to a subagent
  automatically. Nothing already running is stopped.
- If the small model fails or is too slow, your message runs as if the feature
  were off.

## What you see in chat

A card shows the helpers at work: 🔀 running, 🍀 done, 🍂 failed, 🥀 stopped.
Tap the card ("tap to watch 🍀") to follow the steps live, or to read the result
when it is done.

## Change or turn things off

Run these in a terminal (the defaults are shown).

| What | Command |
| --- | --- |
| Turn the whole decision off | `clover config set delegation.router.enabled false` |
| How much it thinks: `none`, `low` (default), `medium`, `high` | `clover config set auxiliary.delegation_router.reasoning_effort low` |
| Pick the small model yourself | `clover config set auxiliary.delegation_router.provider openai-codex` and `clover config set auxiliary.delegation_router.model gpt-6-luna` |
| Wait time when it uses your main model (seconds, default 20) | `clover config set delegation.router.slow_model_timeout_seconds 20` |
| Keep small talk out of the decision (default on) | `clover config set delegation.router.skip_small_talk false` to decide every message |
| Safety net on or off (default on) | `clover config set delegation.router.direct_backstop.enabled false` |
| Safety net: tool calls before hand-off (default 25) | `clover config set delegation.router.direct_backstop.work_calls 25` |
| Safety net: seconds before hand-off (default 180) | `clover config set delegation.router.direct_backstop.seconds 180` |

Developer details:
[delegation-decisions.md](../delegation-decisions.md).
