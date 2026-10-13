# Delegation decisions during planning

Your selected conversation model remains the orchestrator. For substantial
work, it plans first and delegates useful independent execution or research
without requiring you to request subagents. Small tasks can stay direct, and
explicit requests not to use subagents take precedence.

## Choosing how to work: four modes

Each human message is sized before the root starts (see "The router and its
backstop" below) and the root follows the resulting work plan. One short block
in the system prompt (built once, byte-stable for the conversation, along with
the tool schemas) says how to treat that plan; the four ways to work are:

| Mode | When | What the root does |
| --- | --- | --- |
| **Direct** | A quick answer, or brief lookups finished in a couple of minutes with a handful of tool calls; not sustained research or builds | Does it in the chat. |
| **Single helper** | A big job with one thread of work (long report, build, long-running command) | Dispatches one `delegate_task` task with the whole plan in `context`, says in a sentence or two what is running, and ends the turn. |
| **Parallel helpers** | Many items each needing their own research, or independent parts | One task per part, each with its own plan, grouped within tool capacity. |
| **Staged** | Later work needs earlier output (build, then review) | Dispatches the first stage only; when its result returns, checks the artifacts, then dispatches the next stage, carrying scope, acceptance checks and artifact references forward. |

- **What it picks.** The default is direct. Parallel is for fifteen or more
  items that each need their own facts, five or more named products or pages
  each read separately, or unrelated fixes; staged is build -> test -> review
  chains; single is one large cohesive job (long report, a command that runs
  for minutes). A closing merge, ranking, table or test run is never a task of
  its own. Calibrate with `scripts/eval/delegation_router.py` against
  `tests/eval/delegation_modes.jsonl` and the held-out paraphrases in
  `tests/eval/delegation_router_heldout.jsonl` (the rubric is tuned on the
  first only).
- **Preparation before a write or send is a helper job.** Finding, reading and
  checking files, code or past sessions, then assembling or packaging the
  result for a person ("email Julia the extension with exact instructions",
  "also send the X skill") is single; sending a link or text already in the
  chat stays direct. Set `delegation.router.calibration_v2: false` to restore
  the earlier rubric and override wording exactly.
- **The user's words win.** "Do it yourself" (or "do it here / directly")
  means direct even for a big job; "use helpers", "send subagents", "with
  subagents" or "in the background" means a helper even for a small one.
- **Signals for big:** more than a couple of minutes - a dozen or more items
  each needing lookups, commands that run for minutes, multi-step research or
  builds. They live in the router's sizing prompt, not in the system prompt.
- **Every worker is briefed:** scope, inputs, acceptance checks and ownership
  go in the task's `context`.
- **After an accepted dispatch** the root acknowledges and ends the turn; it
  does not poll. A rejected dispatch is not a hand-off: the job is still the
  root's. It verifies the results before saying the work is done.
- **Private internals, honest blockers.** Limits, timeouts and retries are never
  mentioned to the user; a genuine blocker is reported plainly.
- **Who gets what:** a root agent with `delegate_task`, on a surface that can
  deliver a helper's result later (messaging platforms, CLI / TUI), gets the
  block. A root that cannot (cron, one-shot / batch runs, kanban workers,
  webhooks, the API server without a session to wake) gets a short
  delivery-neutral note instead - plan first, delegate where supported,
  integrate and verify, handle quick work directly - with no "end your turn" or
  "don't poll". Subagents, delegated children and agents without
  `delegate_task` get neither. Each is decided once when the prompt is built,
  and none depends on the time limit's setting.
- **Tool description.** `delegate_task` tells the model to omit `tier` unless
  the task is clearly review (`check`) or clearly hard (`deep`), and that long
  scripts, long code cells and many lookups are reasons to hand off rather than
  run inline.

## The router and its backstop

`delegation.router` (on by default) applies exactly where the foreground time
limit does: a root chat agent with `delegate_task` on a surface that can
deliver a helper's result later, with the limit on. It never runs for helper
completions, multimodal messages, or a bare greeting / thank-you.

- **Sizing.** Before the first model call of a human message, a fast auxiliary
  model (`auxiliary.delegation_router`: by default the provider's small fast model, e.g. `gpt-6-luna` on Codex, Haiku on Anthropic; `prefer_fast_model: false` or a pinned provider/model opts out)
  returns `{mode, tasks: [{goal, depends_on, acceptance}], reason}`. The reply
  is schema-validated and the mode is derived from the task DAG in code - no
  tasks is direct, one is single, several independent are parallel, any
  dependency is staged - never taken on the model's word. One retry on an
  invalid reply, a `timeout_seconds` (8) budget, then no plan and the turn runs
  exactly as it did before the router. A warm call answers in about 1.5 s
  (p95 about 3 s); the first call in a fresh process also pays one-time SDK
  imports and can miss the budget, which is the "no plan" path.
- **The user's words win.** "Do it yourself" is direct with no sizing call and
  no backstop; "use helpers" is at least single.
- **Delivery.** The plan is a `[Clover runtime] work plan:` block on the API
  copy of that turn's user message, persisted beside the stored text like
  prefetch context. The stored text stays clean, no message is added, and the
  cached prefix is untouched.
- **Backstop.** When a not-direct plan's root keeps doing the work itself - more
  than `backstop_calls` (2) work tool calls or `backstop_seconds` (60) without
  calling `delegate_task` - its next work call is refused and the runtime
  dispatches the plan through the `delegate_task` handler: one call for all
  independent tasks; for a staged plan the first stage, with the next stage
  bound through `follow_through` where that mechanism can hold it (one task,
  then one task). The refused call's result is the hand-off ("Handed off to
  helpers ..."), after which the root ends its turn. Nothing already running is
  stopped, and what the turn had already gathered goes into each helper's
  context. If the dispatch fails the result says so and the root carries on
  under the time limit below.
- **Direct backstop.** A direct verdict is watched too
  (`delegation.router.direct_backstop`, on by default): the runtime counts the
  root's work calls and collects their findings exactly as above. After
  `work_calls` (25) calls or `seconds` (180), the next work call is refused and
  the rest of the request goes to exactly one helper, built from the user's
  message plus the findings. The same exemptions, scope, refusal and failure
  behaviour apply; "do it yourself" (and a root that already dispatched a helper
  itself) is never handed off again. `enabled: false` restores the old
  behaviour.
- **No plan** (router off, timeout, bad reply) is untouched: the time limit stays the only bound.

```yaml
delegation:
  router:
    enabled: true
    timeout_seconds: 8
    backstop_calls: 2
    backstop_seconds: 60
    direct_backstop:
      enabled: true
      work_calls: 25
      seconds: 180
    calibration_v2: true             # rubric v2 + wider override wording
    slow_model_timeout_seconds: 20   # wait when only the big main model exists
    skip_small_talk: true            # "thanks", "ok go", "lol": no router call
auxiliary:
  delegation_router:
    provider: auto
    model: auto
    prefer_fast_model: true          # false = always the main model
    reasoning_effort: low            # default; none | minimal | low | medium | high
```

When no small model exists for the main provider the router runs on the main
model and waits `slow_model_timeout_seconds` (default 20) instead of
`timeout_seconds` (8); the log says which applied. Pure small talk (an explicit
allow-list, never a length rule) skips the router entirely.

### Reasoning and the hand-off card

The router thinks a little: `auxiliary.delegation_router.reasoning_effort`
defaults to `low`. With reasoning off, `gpt-6-luna` sized a three-part compare
(GPU options, PSU fit, prices) as a quick answer (0 of 2); on `low` the same
model scored 12 of 12 at a 1.5 s median. Only title generation still runs with
reasoning off. Per provider: Codex `reasoning.effort=low`; Gemini
`thinkingLevel=low` (3.x Flash rejects `minimal`); Anthropic adaptive-thinking
models `effort=low`, Haiku and manual-budget models are left as they were. An
explicit `reasoning_effort` (or `extra_body.reasoning`) is never overridden.
`ROUTER_MAX_TOKENS` is 1500 so thought tokens cannot cut off the JSON.

When the backstop (or the time-limit hand-off) hands a turn to a helper, the
chat card names the WORK, never the user's latest message. Two calls on the
router's small model run beside each other. The title call is tiny (the latest
message, the last four turns with the END of a long turn kept, the todo list, a
few hundred characters of findings; `TITLE_TIMEOUT_SECONDS`, 5 s, the dispatch
waits for it) and returns `{"title", "goal"}`: a 3-6 word gerund title
(`🔄 deploying the DND site to dndsecurity.ca`) and a goal of at most 8 words.
The "known so far" bullets are their own, slow call over the whole notes file
(`KNOWN_TIMEOUT_SECONDS`, 25 s); the dispatch waits only `KNOWN_GRACE_SECONDS`
for them, and bullets that land later are written into the notes file the
helper reads first. (They used to share one 6 s call with the title: on a 26k
char notes file it timed out 3 of 3, and the card said `finishing this up`.)
Without a title answer the title comes, in order, from the in-progress todo
item, the plan's own task, the action the assistant's last turn offered when the
message is only a go-ahead (`ok, do it` after `want me to start on the
tracking?`), the chat's last real hand-off title (kept on disk, so a restart
does not lose it), the earlier request this turn continues, else `finishing
this up` -- only when there is truly nothing. A message is used only when it is
itself an order; a question, a complaint, chit-chat or anything with profanity
never becomes a title, and profanity is always stripped.

The line before the card is written by the agent itself, in its own voice: once
the helper is accepted the root takes ONE more model turn over the messages it
already has (same system prompt, a runtime note appended to a copy, tools off)
saying a subagent is now handling the title. If that turn errors, is empty or
takes more than 30 s the line is `on it`. There is no canned wording.

The same line is written when the ROOT called `delegate_task` itself with a
`handoff` but no `say` (and no text of its own beside the call), on a chat
platform. Without it the turn's reply would be the card alone: the user gets
no reply, and the card cannot join a card already running in the chat, since
joining goes through the opener line. That turn is otherwise finished, so the
wait is capped at 15 s (Opus 5.5 answered in 2.6-3.7 s, GPT-6.1 Sol in
5.8-10.6 s). A `say` the model wrote is never doubled. The CLI keeps the whole
card text as the reply.

A helper completion where any task is not done (failed, `Result: not done`,
incomplete, cut off at max iterations) is never offered `[SILENT]`: the prompt
asks for a plain line on what did not get done. A clean completion the user
already heard may still be absorbed with `[SILENT]`.

### Ship gate

Run before changing the router prompt, the router's reasoning, or the shipped
fast-model defaults. 30 cases from real use live in
`tests/fixtures/router_eval/ship_gate.jsonl` (`expected_mode` may be a list
where two modes are both defensible; `"gate": true` marks the cases that
failed live). Copy a profile's `config.yaml` and `auth.json` into a temporary
`CLOVER_HOME` (`chmod 700`), pin `auxiliary.delegation_router` to the model
under test, and run:

```bash
python scripts/eval/delegation_router.py --home "$TMP_HOME" \
  --cases tests/fixtures/router_eval/ship_gate.jsonl \
  --reps 2 --ship-gate --out /tmp/ship-gate-out
```

`--ship-gate` takes each case's own `reps` (3 for the hard ones), requires
accuracy of at least 90 percent and every `gate` case right in at least 2 of 3
runs, and exits 1 on a miss. Do not edit the rubric to fit one model's misses;
report them. The gate is run by hand (it calls real models), not in CI.

## The foreground time limit

Instead of a work-call cap, the main chat agent gets a **time limit per user
message**: `delegation.foreground_time_limit_seconds`, default **300** (5
minutes). `0`, `null`, `false`, a boolean or any invalid value turns it off.
The goal: a message gets a final answer close to that limit, and whatever is
left is already running in a background helper.

```yaml
delegation:
  foreground_time_limit_seconds: 300
```

- **Clock.** Monotonic; it starts when a turn that consumes a human message
  begins. A turn that delivers a helper's result (an `internal_notification`
  turn carrying the gateway's `completion_delegation_ids`) is the continuation
  of the user's work and starts its OWN full clock, like a new message; it
  never inherits the spent clock of the request that dispatched the helper.
  Other internal turns (watches, restart wake-ups) never start a clock: they
  inherit the episode's remaining time, and when none is left (or a freshly
  restarted process has no episode at all) they run in finish mode from the
  start, which still allows reads and the final answer. Steering or redirecting a running turn does not reset it; a
  follow-up message gets a fresh 5 minutes when it is consumed as its own turn.
- **Up front, and quiet.** The limit is a backstop. The mode-choice block
  (above) already tells the agent to hand clearly big jobs to a background
  helper at the start and to do quick things itself. Every model-facing limit
  text (notice, refusal, forced-background result) tells the model not to
  mention the limit, timeouts or failed attempts to the user. The hard-stop
  request asks for a plain summary that keeps internal mechanics out of the
  reply; it is stored with `display_kind="hidden"` (like the max-iterations
  request) so no transcript, resume or export shows it as something the user
  wrote, while the provider still replays it unchanged. The hard-stop fallback
  wording the user can read is neutral and never carries an error message. A
  refused tool shows as "skipped" on the CLI activity feed and in TUI /
  desktop tool rows; only the model reads the refusal text.
- **80%.** One notice, attached only to a tool result produced by the current
  iteration that has not been sent yet (never to an earlier turn or a
  caller-owned message): hand the rest to a background helper with
  `delegate_task` (`background=true`, full context), tell the user, and end the
  reply, unless the rest is a quick finish. Without `delegate_task` the notice
  says to finish now.
- **100% ("finish mode").** For the rest of the turn the following are refused:
  heavy work tools (`terminal` in the foreground, `execute_code`, browser and
  computer use, `web_search` / `web_extract`, `write_file` / `patch`, MCP
  tools), a foreground `delegate_task`, and `process wait`. Always open:
  background `delegate_task`, `cronjob`, `todo`, `memory`, `clarify`,
  `process` poll / log / kill / list, `read_file` / `search_files`,
  `skill_view`, message-sending tools, a background `terminal` command, and the
  final text answer. Helpers cannot use `cronjob`, so the root keeps it for
  jobs such as "update me every 30 minutes".
- **Hard stop.** 60 seconds after the limit, or 3 more model iterations,
  whichever comes first, the loop makes one final no-tools call (the same
  summary path as the max-iterations limit, with its own exit reason
  `foreground_time_limit_hard_stop`) and ends the turn. Tools admitted before
  the limit run to completion under their own timeouts; the stop applies at
  the next iteration boundary. A composed answer a verification gate was
  holding back is used instead of a new call; queued steer / human input and
  transcript persistence behave as on any other turn end.
  **Cache note:** that final call carries no tool schema, so it is the one
  provider request of the turn whose prefix differs from the earlier ones - one
  prompt-cache miss, accepted once per hard stop. (It is a miss for the whole
  prefix, not just the tool list: the summary path re-serializes earlier tool
  calls' arguments from their raw strings, while the loop sends the canonical
  form, so even the message bytes differ.) Turns that finish on their own never
  pay it.
- **Completion turns.** The clock above is the whole story for a helper's
  result: there is no separate "checking window" setting any more
  (`verify_time_limit_seconds` was removed; it gave a spent clock 120 s back
  once per dispatch and was only granted to the instance that dispatched). The
  ids are trusted gateway metadata, never notification text, and no state on
  the agent instance is needed, so a rebuilt agent or a restart gets the same
  fresh clock. If a completion turn itself runs out of time it may hand off to
  a helper at most once per original request; the completion of that hand-off
  ends with the plain summary.
- **Where it applies.** The limit exists to hand work to a background helper,
  so it applies only in chats where the bot can actually run one and deliver
  its result later: Telegram and the other messaging platforms, and the
  interactive CLI / TUI (including `-q` on a real terminal, which seeds an
  interactive session). Where a background helper cannot be delivered - the
  API server, unless the request carries a session id it can be woken by - the
  limit is simply **off** for that message: no notice, no refusals, no hard
  stop, and delegation behaves exactly as before. That decision is made once
  when the human message starts and is inherited by the watch / wake-up
  turns that follow it (a completion turn on such a surface stays off too).
- **Whole-turn hand-off.** While the limit applies to a turn, every
  `delegate_task` of the root runs as a background helper (the tool result says
  so). A helper that cannot be started (capacity, persistence, scheduling,
  construction) is reported as an error to the model; it is never silently run
  inline.
- **Codex app-server.** Codex runs its own tools, so they cannot be refused
  mid-turn; the whole turn is bounded instead. Its turn timeout becomes
  `min(its own 600 s, the episode's remaining time)` (never less than the
  60-second finish window). At expiry the native turn is interrupted through
  the normal interrupt path - even if it had already emitted an assistant
  message - and answered with the last assistant text Codex produced (or a
  plain "stopped at the time limit" note), without retiring the session or
  losing the user's next message.
- **Scope.** Only root conversational agents are limited: gateway messaging
  sessions and the interactive CLI / TUI. Subagents, cron runs,
  `--oneshot` / `-z` runs, `-q` / `-Q` without a terminal, batch and eval
  runners, review forks and kanban workers are never limited. The limit holds
  even if `delegate_task` is not available to the agent; the notice then says
  to finish now.

It is independent of the checkpoint below (either can be on alone) and of
`agent.run_budget_seconds`, which keeps its existing behavior for `--run-budget`
workers and evals (its notice uses the same unsent-tool-result channel).

## The optional checkpoint

The checkpoint is **off by default**: the foreground agent has no cap on work
calls or time. Opt in with `delegation.checkpoint.enabled: true`. Everything
below describes the behavior once it is enabled.

When a conversational root agent has both `todo` and `delegate_task`, the
runtime will not run its first *work* tool of a task until the agent has
recorded a delegation choice through the existing `todo` tool:

- `direct`: do the work here. Give a brief operational reason, such as a small
  scope, tightly dependent steps, or the user's explicit no-subagent request.
- `delegate`: give a reason **and** actually start a child with
  `delegate_task`. The choice alone does not unlock work. Listing, steering or
  stopping children, a malformed call, or a dispatch that fails before any
  child starts does not count. An accepted background child counts, and it takes
  over the rest of the request (see below) rather than unlocking more root
  work. A background request that cannot be had (no delivery path, pool full) is
  reported as `rejected` and nothing runs inline instead. An explicitly
  synchronous child that actually starts also counts and spends one work unit.
  The root does not wait for a background child to finish.

The call is `todo` with a `delegation` argument holding `mode` and a non-empty
`reason`. No checklist is needed: a decision-only call is enough for a one-step
job. The reason is operational metadata, not private reasoning, and is kept
separate from the visible checklist, so task panels and delegation cards do not
change.

A blocked work tool is not run and is not replayed later. It returns a
structured error, `delegation_decision_required` or
`delegation_dispatch_required`, that names the exact `todo` call to make. These
policy blocks are not tool failures: they do not feed the repeated-tool-error
halt and they send no extra message to the user.

### What counts as work

Everything except the control plane: `todo`, `skill_view`, `skills_list`,
`tool_search`, `tool_describe`, `clarify` and `delegate_task`. File, terminal,
browser, `execute_code`, memory and skill-write tools, and unknown, new and MCP
tools are work. A deferred tool reached through `tool_call` is classified by the
tool it resolves to, not by the wrapper. Pure text replies never need a
decision.

### The foreground allowance belongs to the request

A human request gets one foreground allowance: at most 5 parent work calls, or
120 seconds after the first of them. It is not renewable. Declaring again,
restating the choice, renaming the todo, naming a new phase, a late declaration
completion and context compression all leave it untouched; only a new human
message starts a new allowance. Once it is spent the next work call is blocked
with `delegation_foreground_exhausted`. Time is measured on a monotonic clock
from the first work call, so thinking time before work does not count. Calls
that did not run do not count: policy blocks, control-plane calls, plugin or
guardrail denials, and calls refunded because an ACP edit or a terminal command
was denied or left pending approval. Calls that ran and failed do count. A
child's own tool calls do not count, but a synchronous child start spends one
unit, so a chain of synchronous children is bounded too.

From an exhausted request the only way out is a background `delegate_task`
dispatch (the runtime decides whether a root's dispatch is background; the
`background` argument is never consulted). If the user said not to use
subagents, the agent stops and reports an honest status instead.

An accepted background dispatch hands the rest of the request to the worker.
The checkpoint records who owns it (request, goals, subagent ids, the reason in
force and the durable result row ids) and closes the root's work tools
(`delegation_handoff_active`) and further spawning (`delegation_spawn_closed`)
for that request. A dispatch that is rejected, or whose ticket belongs to an
older request, hands off nothing and never cancels the job. `list`, `steer` and
`stop` stay available in every phase.

A new human message starts a new task, so the choice starts undecided again.
Within a turn, extra model iterations and `/steer` do not reset it. History
restored after a restart never carries a choice into a new request.

### Ending the root's turn, and integrating the result

An accepted handoff ends the root's turn through the normal conversation-loop
exit: the loop appends one deterministic assistant message (it names the job and
its goals, says it runs independently and that the result returns here, and
claims nothing is done), makes no further provider call, cancels nothing, and
still runs the usual turn-end persistence and learning. That message is the
handoff card: `delegated:` shows the model's `handoff.work` and `goal:` its
`handoff.outcome`, lightly normalized (first line only, markdown markers,
control characters, absolute path prefixes and internal job ids removed, long
text cut at a word boundary with an ellipsis). Ordinary punctuation such as
`;`, `/`, `:` or quotes is kept as written. When a field is missing, the card
uses the task goal's first sentence instead; it never shows generic "details
unavailable" copy for an accepted dispatch. The gateway releases the
busy slot as it always does, so the next human message is handled normally while
the detached job keeps running. If the allowance is spent and the model keeps
asking for blocked calls, the first blocked assistant message gets one more
provider call so the model can write its own status; a second ends the turn with
a deterministic text that states only what the runtime knows (the allowance is
spent, the calls were not run, and whether a background job of this request is
still running).

A trusted gateway delivery (`internal_notification`) keeps the stored ledger as
it stands. It may additionally open one bounded verification window, but only
when the durable row of a job this request handed off is terminal and its text
was claimed or delivered (a fan-out's receipts are its per-child rows). The
in-memory job status is never consulted, event text is never parsed, a replayed
receipt grants nothing, and each request opens at most
`max_integration_windows` windows. A window has the same size as the base
allowance, spends its own budget (never the stored ledger, so a late result
cannot alter a newer request), and cannot spawn. Receipts beyond the cap and a
fan-out's batch-level join event still arrive as text but open no window.
CLI and TUI deliveries cannot prove their origin; they cost a fresh request.

### Who is exempt

Delegated children and orchestrator children, agents missing `todo` or
`delegate_task`, the cron platform, a dispatcher-spawned kanban worker, the
background review fork that powers memory and skill learning (the `/btw`
side-question fork comes from the same factory and is exempt too), and roots whose
caller marked them noninteractive (`batch_runner`, one-shot runs). Markers are
explicit properties set by the code that creates the agent, never inferred from
message text. For these agents `todo` stays optional.

### Configuration

```yaml
delegation:
  checkpoint:
    enabled: false           # default; true opts in to gating
    max_work_tools: 5        # positive integer
    max_foreground_seconds: 120   # positive number
    max_integration_windows: 2    # positive integer, per handed-off request
```

Invalid, boolean, non-finite or non-positive values fall back to the defaults,
never to a zero budget. Changes take effect at the start of the next turn.
When enabled, it applies to every eligible root, not only to a trial agent.

## What this does not guarantee

The checkpoint guarantees that a choice was made and, for `delegate`, that a
child really started. It does not judge the quality of the stated reason, pick
for the model, or classify task size; a model that always answers `direct` with
a plausible reason is allowed to. There is no worker quota and no completion
blocker.

Known limits:

- External gateway adapters that mark an inbound event `internal` (for example
  a webhook) keep the stored ledger across that delivery. They gain a window
  only by presenting a durable claimed receipt of a job the request handed off.
- Admission is checked at the next call. A tool call already in flight, or a
  provider stall, cannot be pre-empted; the loop exit happens after the current
  round's results are canonical.
- The ledger, owned handoffs and windows are process-local. After an agent
  eviction or restart a late result arrives as text only.
- The TUI's `async_delegation_complete` delivery has a trusted origin but is
  deliberately not widened; it resets like any other entry.
- A `todo` declaration takes effect only when the foreground accepts its normal
  completion. A declaration that was held by middleware or a plugin hook and then
  timed out, was cancelled or abandoned, crashed, was denied, or belongs to an
  older turn or checkpoint may still update the plan metadata, but it never
  grants authority, even if it finishes later in the same turn. Out-of-tree code
  that runs the `todo` tool on its own abandoned thread without the executor's
  private context is outside this contract; it is not made safe by it.
- A work call is checked when it is about to run. A tool that is already
  running is never aborted when its budget runs out.
- Starting a child does not by itself return control to the user. After
  dispatching background workers the agent should end its turn with a short
  acknowledgement. Time the parent then spends compacting its own context is a
  separate problem, not addressed here.

The runtime never rebuilds the system prompt, replaces earlier messages,
switches the model, or injects synthetic user messages to perform the check.
Runtime errors reach agents that started before an upgrade; the static
instructions teach the rule to new sessions.

This change does not modify updater, restart, process-termination, or Windows
update behavior. Windows `/update` acceptance is a separate verification step.
