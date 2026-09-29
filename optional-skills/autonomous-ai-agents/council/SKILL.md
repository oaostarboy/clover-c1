---
name: council
description: Convene an adversarial multi-model decision council.
version: 0.1.0
author: Anthony Nguyen (oaostarboy), Clover Cognition
license: MIT
platforms: [linux, macos]
metadata:
  clover:
    tags: [Council, Multi-Agent, Decisions, Review]
    related_skills: [clover-c1]
---

# Council Skill

Convene independent AI seats with conflicting mandates, anonymously cross-review
their answers, and have a chairman commit to one verdict. This is a simulated
council of models, not a panel of people or independent legal/financial experts.

## When to Use

- The user says “ask the council,” “have the council review this,” or `/council`.
- A decision benefits from deliberate disagreement instead of one agreeable pass.
- Use `deep` before costly, public, or hard-to-reverse actions.
- Don't use for routine facts, arithmetic, or tasks the user already decided.

## Prerequisites

- Clover CLI is available as `clover` or through `CLOVER_BIN`.
- Every provider in `references/models.json` is authenticated.
- The installed skill lives under `$CLOVER_HOME/skills/autonomous-ai-agents/council`.

## How to Run

Telegram and gateway syntax (this path shows one live `🏛 Council` card and ends as a single message):

- `/council <question>` — full council.
- `/council quick <question>` — three blind seats, then chairman.
- `/council deep <question>` — six seats, cross-review, chairman, adversarial
  attack, and chairman re-ruling when the attack is serious.

For a natural-language request, choose `full` unless the user explicitly asks
for quick or deep. Invoke through `terminal`:

```text
python "$CLOVER_HOME/skills/autonomous-ai-agents/council/scripts/council_run.py" \
  --mode full "<the exact question>"
```

Run it exactly like this (only `--mode` changes): the runner writes to
`$CLOVER_HOME/council/runs/<id>/`, the same place `/council` uses. In a gateway
chat (Telegram, Discord, Slack, ...) it also records which chat asked, and the
gateway then shows one live `🏛 Council` card in that chat (question, each seat
and its model, following the latest message) and, when the run ends, replaces it
with ONE final message that holds the seats' takes and the answer. That happens
automatically, once per run. Do not paste the card or the answer again; add only
your own view, labeled as yours.

Use a timeout of at least 1800 seconds. When the gateway owns delivery the runner
prints `COUNCIL_DONE` and an absolute `COUNCIL_REPORT=` path, not the verdict, so
there is nothing to relay. Read the report or `summary.json` only when the user
asks for the complete debate. From the plain CLI (no card) the runner prints the
verdict, reason, and caveat: return them in three short sections, **Answer**,
**Why**, and **What could change it**, with the mode and any stalled seats. Never
paste the chairman's full analysis into the chat unless the user explicitly asks.

## Seats

- **STEELMAN** — strongest honest case for.
- **PROSECUTOR** — fatal flaw, hidden cost, concrete blow-up.
- **PREMISE** — challenges whether this is the real question.
- **PRAGMATIST** — smallest version that works.
- **OUTSIDER** — assumes no missing context.
- **HISTORIAN** — checks Clover memory and cites what it finds.
- **CHAIRMAN** — weighs conflict and commits.
- **ATTACK** — deep-mode attack on the written verdict.

The exact provider/model map is `references/models.json`. Never silently replace
an unavailable seat with the current model. A missing route is a stalled seat and
must be disclosed.

## Procedure

1. Preserve the user's question exactly. Do not improve the premise before the
   PREMISE seat sees it.
2. Run the script once in the requested mode. Completion means it exits zero and
   prints `COUNCIL_REPORT=`.
3. Report the chairman's final `VERDICT`, `WHY`, and `CAVEAT`. State missing or
   stalled seats. Completion means the user can distinguish council output from
   your own recommendation.
4. If the runner fails, state the failed stage and provider/seat. Do not answer as
   though the council met.

## Pitfalls

- A route ping proves reachability, not role fitness.
- `quick` skips anonymous cross-review and must not be presented as high confidence.
- Only `deep` runs ATTACK. A serious attack returns to CHAIRMAN; ATTACK never owns
  the final decision.
- Seats can use tools and edit files. Their prompts restrict them to their output
  file, but run the council only on questions whose working context is safe.
- This runner uses process groups and file-settling logic and is therefore gated
  to Linux/macOS.

## Verification

A successful run must have all three:

- process exit code `0`;
- a report path that exists;
- parseable `VERDICT`, `WHY`, and `CAVEAT` lines.

For route verification, run the quick council on a reversible test question and
confirm the report names the configured model for every returned seat.
