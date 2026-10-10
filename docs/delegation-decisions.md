# Delegation decisions during planning

Your selected conversation model remains the orchestrator. For substantial
work, it plans first and delegates useful independent execution or research
without requiring you to request subagents. Small tasks can stay direct, and
explicit requests not to use subagents take precedence.

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
  begins. Helper completions, watches and restart wake-ups
  (`internal_notification` turns) never start a clock: they inherit the
  episode's remaining time, and when none is left (or a freshly restarted
  process has no episode at all) they run in finish mode from the start, which
  still allows reads and the final answer so a helper's result can be
  integrated. Steering or redirecting a running turn does not reset it; a
  follow-up message gets a fresh 5 minutes when it is consumed as its own turn.
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
- **Where it applies.** The limit exists to hand work to a background helper,
  so it applies only in chats where the bot can actually run one and deliver
  its result later: Telegram and the other messaging platforms, and the
  interactive CLI / TUI (including `-q` on a real terminal, which seeds an
  interactive session). Where a background helper cannot be delivered - the
  API server, unless the request carries a session id it can be woken by - the
  limit is simply **off** for that message: no notice, no refusals, no hard
  stop, and delegation behaves exactly as before. That decision is made once
  when the human message starts and is inherited by the helper-completion
  turns that follow it.
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
